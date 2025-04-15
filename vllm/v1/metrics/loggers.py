# SPDX-License-Identifier: Apache-2.0

import time
from abc import ABC, abstractmethod
from typing import Optional
import json
import os
import datetime
import threading

import numpy as np
import prometheus_client

from vllm.config import SupportsMetricsInfo, VllmConfig
from vllm.logger import init_logger
from vllm.v1.core.kv_cache_utils import PrefixCachingMetrics
from vllm.v1.engine import FinishReason
from vllm.v1.metrics.stats import IterationStats, SchedulerStats
from vllm.v1.spec_decode.metrics import SpecDecodingMetrics

logger = init_logger(__name__)

_LOCAL_LOGGING_INTERVAL_SEC = 5.0


class StatLoggerBase(ABC):

    @abstractmethod
    def record(self, scheduler_stats: SchedulerStats,
               iteration_stats: Optional[IterationStats]):
        ...

    def log(self):  # noqa
        pass


class LoggingStatLogger(StatLoggerBase):

    def __init__(self, engine_index: int = 0):
        self.engine_index = engine_index
        self._reset(time.monotonic())
        self.last_scheduler_stats = SchedulerStats()
        # Prefix cache metrics. This cannot be reset.
        # TODO: Make the interval configurable.
        self.prefix_caching_metrics = PrefixCachingMetrics()
        self.spec_decoding_metrics = SpecDecodingMetrics()

    def _reset(self, now):
        self.last_log_time = now

        # Tracked stats over current local logging interval.
        self.num_prompt_tokens: list[int] = []
        self.num_generation_tokens: list[int] = []

    def _track_iteration_stats(self, iteration_stats: IterationStats):
        # Save tracked stats for token counters.
        self.num_prompt_tokens.append(iteration_stats.num_prompt_tokens)
        self.num_generation_tokens.append(
            iteration_stats.num_generation_tokens)
        self.prefill_time = sum([req.prefill_time for req in iteration_stats.finished_requests])

    def _get_throughput(self, tracked_stats: list[int], now: float) -> float:
        # Compute summary metrics for tracked stats
        return float(np.sum(tracked_stats) / (now - self.last_log_time))

    def record(self, scheduler_stats: SchedulerStats,
               iteration_stats: Optional[IterationStats]):
        """Log Stats to standard output."""

        if iteration_stats:
            self._track_iteration_stats(iteration_stats)

        self.prefix_caching_metrics.observe(scheduler_stats.prefix_cache_stats)

        if scheduler_stats.spec_decoding_stats is not None:
            self.spec_decoding_metrics.observe(
                scheduler_stats.spec_decoding_stats)

        self.last_scheduler_stats = scheduler_stats

    def log(self):
        now = time.monotonic()
        prompt_throughput = self._get_throughput(self.num_prompt_tokens, now)
        generation_throughput = self._get_throughput(
            self.num_generation_tokens, now)

        self._reset(now)

        scheduler_stats = self.last_scheduler_stats

        # Format and print output.
        logger.info(
            "Engine %03d: "
            "Avg prompt throughput: %.1f tokens/s, "
            "Avg generation throughput: %.1f tokens/s, "
            "Running: %d reqs, Waiting: %d reqs, "
            "GPU KV cache usage: %.1f%%, "
            "Prefix cache hit rate: %.1f%%",
            self.engine_index,
            prompt_throughput,
            generation_throughput,
            scheduler_stats.num_running_reqs,
            scheduler_stats.num_waiting_reqs,
            scheduler_stats.gpu_cache_usage * 100,
            self.prefix_caching_metrics.hit_rate * 100,
        )

        if scheduler_stats.spec_decoding_stats is not None:
            self.spec_decoding_metrics.log()

class CacheTelemetryLogger(StatLoggerBase):
    """
    Records detailed prefix cache statistics for all engines periodically
    to a single JSON file, including aggregated totals.
    """
    def __init__(self, engine_index: int = 0,output_dir: str = "vllm_cache_telemetry_output"):
        self.per_engine_metrics: dict[int, PrefixCachingMetrics] = {}
        self.output_dir = output_dir
        self.prefill_time = 0
        os.makedirs(self.output_dir, exist_ok=True)
        self.filepath = os.path.join(self.output_dir, "cache_telemetry.json")
        logger.info(f"CacheTelemetryLogger initialized. Outputting combined stats to: {self.filepath}")
        # Create the empty file
        with open(self.filepath, "w") as f:
            json.dump({}, f, indent=4)

    def record(self, engine_index: int, scheduler_stats: SchedulerStats,
               iteration_stats: Optional[IterationStats]):
        if scheduler_stats and scheduler_stats.prefix_cache_stats:
            if engine_index not in self.per_engine_metrics:
                self.per_engine_metrics[engine_index] = PrefixCachingMetrics()
                logger.info(f"First stats received from engine {engine_index}. Initializing metrics.")
            self.per_engine_metrics[engine_index].observe(scheduler_stats.prefix_cache_stats)
            
        if iteration_stats:
            self.prefill_time += sum([req.prefill_time for req in iteration_stats.finished_requests])

    def _calculate_total_stats(self) -> dict:
        """Helper method to aggregate stats across all engines."""
        if not self.per_engine_metrics:
            empty_stats = PrefixCachingMetrics().get_stats()
            empty_stats.setdefault("request_level", {})["request_evictions"] = 0
            return empty_stats

        total_metrics = PrefixCachingMetrics(interval=0)

        for engine_metrics in self.per_engine_metrics.values():
            total_metrics.aggregated_requests += engine_metrics.aggregated_requests
            total_metrics.aggregated_query_total += engine_metrics.aggregated_query_total
            total_metrics.aggregated_query_hit += engine_metrics.aggregated_query_hit
            total_metrics.aggregated_block_eviction += engine_metrics.aggregated_block_eviction
            total_metrics.aggregated_request_hit += engine_metrics.aggregated_request_hit

        total_stats = total_metrics.get_stats()

        return total_stats

    def log(self):
        """Get aggregated cache stats for all engines and totals,
           and write them to a single JSON file."""
           
        # Check if file is present. If it is not present reset all the stats
        if not os.path.exists(self.filepath):
            logger.info(f"CacheTelemetryLogger: File not found, resetting all stats.")
            self.per_engine_metrics = {}
            self.prefill_time = 0

        engine_stats_dict = {}
        sorted_engine_indices = sorted(self.per_engine_metrics.keys())

        for engine_idx in sorted_engine_indices:
            engine_metrics = self.per_engine_metrics[engine_idx]
            stats = engine_metrics.get_stats()
            engine_stats_dict[engine_idx] = stats

        total_stats = self._calculate_total_stats()

        output_data = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"), # Added microseconds
            "engines": engine_stats_dict,
            "total": total_stats,
            "prefill_time": self.prefill_time,
        }

        with open(self.filepath, "w") as f:
            json.dump(output_data, f, indent=4)
            
class CacheTelemetry:
    """
    Track cache hit/miss statistics at both request and block levels.
    """

    _file_lock = threading.Lock()

    def __init__(self, output_dir: str, reset_cache_telemetry_on_new_file: bool = True):
        print("[DEBUG] CacheTelemetry: Initializing telemetry tracking")
        self.output_dir = output_dir
        self.reset_cache_telemetry_on_new_file = reset_cache_telemetry_on_new_file
        self.init_time = time.time()
        self.reset()

    def reset(self):
        print("[DEBUG] CacheTelemetry: Resetting all telemetry counters")

        # block
        self.total_blocks = 0
        self.total_hits = 0
        self.total_misses = 0
        self.total_evictions = 0
        self.first_block = True

        # block (time series)
        self.total_blocks_ts = [] # (timestamp, num_blocks)
        self.total_hits_ts = [] # (timestamp, num_hits)
        self.total_misses_ts = [] # (timestamp, num_misses)
        self.total_evictions_ts = [] # (timestamp, num_evictions)

        # requests
        self.unique_requests = 0
        self.requests_with_hits = set()
        self.requests_with_misses = set()
        self.requests_with_evictions = set()

        self.tracked_requests = set()   # To keep track of unique request IDs

        # requests (time series)
        self.unique_requests_ts = [] # (timestamp, num_requests)
        self.requests_with_hits_ts = [] # (timestamp, num_hits)
        self.requests_with_misses_ts = [] # (timestamp, num_misses)
        self.requests_with_evictions_ts = [] # (timestamp, num_evictions)

        self.init_time = time.time()

    def record_hit(self, num_blocks: int, request_id=None):
        if self.first_block:
            # ignore first block because it will always hit
            self.first_block = False
            return

        if request_id is not None:
            if request_id not in self.tracked_requests:
                print(f"[DEBUG] CacheTelemetry: Tracking new request ID: {request_id}")
                self.unique_requests += 1
                self.tracked_requests.add(request_id)
                self.unique_requests_ts.append((time.time() - self.init_time, 1))
            if request_id not in self.requests_with_hits:
                self.requests_with_hits.add(request_id)
                self.requests_with_hits_ts.append((time.time() - self.init_time, 1))

        if num_blocks > 0:
            # print(f"[DEBUG] HIT request_id: {request_id}, num_blocks: {num_blocks}")
            self.total_blocks += num_blocks
            self.total_hits += num_blocks

            # record time series
            timestamp = time.time() - self.init_time
            self.total_blocks_ts.append((timestamp, num_blocks))
            self.total_hits_ts.append((timestamp, num_blocks))

    def record_miss(self, num_blocks: int, request_id=None):
        if request_id is not None:
            if request_id not in self.tracked_requests:
                self.unique_requests += 1
                self.tracked_requests.add(request_id)
                self.unique_requests_ts.append((time.time() - self.init_time, 1))
            if request_id not in self.requests_with_misses:
                self.requests_with_misses.add(request_id)
                self.requests_with_misses_ts.append((time.time() - self.init_time, 1))

        if num_blocks > 0:
            # print(f"[DEBUG] MISS request_id: {request_id}, num_blocks: {num_blocks}")
            self.total_blocks += num_blocks
            self.total_misses += num_blocks

            # record time series
            timestamp = time.time() - self.init_time
            self.total_blocks_ts.append((timestamp, num_blocks))
            self.total_misses_ts.append((timestamp, num_blocks))

    def record_eviction(self, num_blocks: int, request_id=None):
        self.total_evictions += num_blocks

        # record time series
        timestamp = time.time() - self.init_time
        self.total_evictions_ts.append((timestamp, num_blocks))

        if request_id is not None:
            if request_id not in self.tracked_requests:
                self.unique_requests += 1
                self.tracked_requests.add(request_id)
                self.unique_requests_ts.append((timestamp, 1))
            # this path is dead as of now
            if request_id not in self.requests_with_evictions:
                self.requests_with_evictions.add(request_id)
                self.requests_with_evictions_ts.append((timestamp, 1))

            # record time series
            self.requests_with_evictions_ts.append((timestamp, num_blocks))

    def get_all_stats(self) -> dict:

        return {
            "block_level": {
                "total_blocks": self.total_blocks if self.total_blocks > 0 else 0,
                "hits": self.total_hits if self.total_hits > 0 else 0,
                "misses": self.total_misses,
                "evictions": self.total_evictions,
                "hit_rate": self.total_hits / self.total_blocks if self.total_blocks > 0 else 0.,
                "miss_rate": self.total_misses / self.total_blocks if self.total_blocks > 0 else 0.,
            },
            "request_level": {
                "unique_requests": self.unique_requests,
                "hits": len(self.requests_with_hits),
                "misses": len(self.requests_with_misses),
                "evictions": len(self.requests_with_evictions),
                "hit_rate": len(self.requests_with_hits) / self.unique_requests if self.unique_requests > 0 else 0.,
                "miss_rate": len(self.requests_with_misses) / self.unique_requests if self.unique_requests > 0 else 0.,
            },
            "block_level_ts": {
                "total_blocks": self.total_blocks_ts,
                "hits": self.total_hits_ts,
                "misses": self.total_misses_ts,
                "evictions": self.total_evictions_ts,
            },
            "request_level_ts": {
                "unique_requests": self.unique_requests_ts,
                "hits": self.requests_with_hits_ts,
                "misses": self.requests_with_misses_ts,
                "evictions": self.requests_with_evictions_ts,
            },
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def record_stats(self):
        # write to disk with safety measures
        stats = self.get_all_stats()

        with CacheTelemetry._file_lock:
            try:
                os.makedirs(self.output_dir, exist_ok=True)

                filepath = os.path.join(self.output_dir, "cache_telemetry.json")

                if not os.path.exists(filepath) and self.reset_cache_telemetry_on_new_file:
                    self.reset()

                with open(filepath, "w") as f:
                    json.dump(stats, f, indent=4)

            except (IOError, OSError) as e:
                logger.warning(f"Failed to write cache telemetry stats: {e}")

class PrometheusStatLogger(StatLoggerBase):

    def __init__(self, vllm_config: VllmConfig, engine_index: int = 0):
        self._unregister_vllm_metrics()

        # Use this flag to hide metrics that were deprecated in
        # a previous release and which will be removed future
        self.show_hidden_metrics = \
            vllm_config.observability_config.show_hidden_metrics

        labelnames = ["model_name", "engine"]
        labelvalues = [
            vllm_config.model_config.served_model_name,
            str(engine_index)
        ]

        max_model_len = vllm_config.model_config.max_model_len

        #
        # Scheduler state
        #
        self.gauge_scheduler_running = prometheus_client.Gauge(
            name="vllm:num_requests_running",
            documentation="Number of requests in model execution batches.",
            labelnames=labelnames).labels(*labelvalues)

        self.gauge_scheduler_waiting = prometheus_client.Gauge(
            name="vllm:num_requests_waiting",
            documentation="Number of requests waiting to be processed.",
            labelnames=labelnames).labels(*labelvalues)

        #
        # GPU cache
        #
        self.gauge_gpu_cache_usage = prometheus_client.Gauge(
            name="vllm:gpu_cache_usage_perc",
            documentation="GPU KV-cache usage. 1 means 100 percent usage.",
            labelnames=labelnames).labels(*labelvalues)

        self.counter_gpu_prefix_cache_queries = prometheus_client.Counter(
            name="vllm:gpu_prefix_cache_queries",
            documentation=
            "GPU prefix cache queries, in terms of number of queried blocks.",
            labelnames=labelnames).labels(*labelvalues)

        self.counter_gpu_prefix_cache_hits = prometheus_client.Counter(
            name="vllm:gpu_prefix_cache_hits",
            documentation=
            "GPU prefix cache hits, in terms of number of cached blocks.",
            labelnames=labelnames).labels(*labelvalues)

        #
        # Counters
        #
        self.counter_num_preempted_reqs = prometheus_client.Counter(
            name="vllm:num_preemptions_total",
            documentation="Cumulative number of preemption from the engine.",
            labelnames=labelnames).labels(*labelvalues)

        self.counter_prompt_tokens = prometheus_client.Counter(
            name="vllm:prompt_tokens_total",
            documentation="Number of prefill tokens processed.",
            labelnames=labelnames).labels(*labelvalues)

        self.counter_generation_tokens = prometheus_client.Counter(
            name="vllm:generation_tokens_total",
            documentation="Number of generation tokens processed.",
            labelnames=labelnames).labels(*labelvalues)

        self.counter_request_success: dict[FinishReason,
                                           prometheus_client.Counter] = {}
        counter_request_success_base = prometheus_client.Counter(
            name="vllm:request_success_total",
            documentation="Count of successfully processed requests.",
            labelnames=labelnames + ["finished_reason"])
        for reason in FinishReason:
            self.counter_request_success[
                reason] = counter_request_success_base.labels(*(labelvalues +
                                                                [str(reason)]))

        #
        # Histograms of counts
        #
        self.histogram_num_prompt_tokens_request = \
            prometheus_client.Histogram(
                name="vllm:request_prompt_tokens",
                documentation="Number of prefill tokens processed.",
                buckets=build_1_2_5_buckets(max_model_len),
                labelnames=labelnames).labels(*labelvalues)

        self.histogram_num_generation_tokens_request = \
            prometheus_client.Histogram(
                name="vllm:request_generation_tokens",
                documentation="Number of generation tokens processed.",
                buckets=build_1_2_5_buckets(max_model_len),
                labelnames=labelnames).labels(*labelvalues)

        self.histogram_iteration_tokens = \
            prometheus_client.Histogram(
                name="vllm:iteration_tokens_total",
                documentation="Histogram of number of tokens per engine_step.",
                buckets=build_cudagraph_buckets(vllm_config),
                labelnames=labelnames).labels(*labelvalues)

        self.histogram_max_num_generation_tokens_request = \
            prometheus_client.Histogram(
                name="vllm:request_max_num_generation_tokens",
                documentation=
                "Histogram of maximum number of requested generation tokens.",
                buckets=build_1_2_5_buckets(max_model_len),
                labelnames=labelnames).labels(*labelvalues)

        self.histogram_n_request = \
            prometheus_client.Histogram(
                name="vllm:request_params_n",
                documentation="Histogram of the n request parameter.",
                buckets=[1, 2, 5, 10, 20],
                labelnames=labelnames).labels(*labelvalues)

        self.histogram_max_tokens_request = \
            prometheus_client.Histogram(
                name="vllm:request_params_max_tokens",
                documentation="Histogram of the max_tokens request parameter.",
                buckets=build_1_2_5_buckets(max_model_len),
                labelnames=labelnames).labels(*labelvalues)

        #
        # Histogram of timing intervals
        #
        self.histogram_time_to_first_token = \
            prometheus_client.Histogram(
                name="vllm:time_to_first_token_seconds",
                documentation="Histogram of time to first token in seconds.",
                buckets=[
                    0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25, 0.5,
                    0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 20.0, 40.0, 80.0, 160.0,
                    640.0, 2560.0
                ],
                labelnames=labelnames).labels(*labelvalues)

        self.histogram_time_per_output_token = \
            prometheus_client.Histogram(
                name="vllm:time_per_output_token_seconds",
                documentation="Histogram of time per output token in seconds.",
                buckets=[
                    0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5,
                    0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 20.0, 40.0, 80.0
                ],
                labelnames=labelnames).labels(*labelvalues)

        request_latency_buckets = [
            0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0,
            40.0, 50.0, 60.0, 120.0, 240.0, 480.0, 960.0, 1920.0, 7680.0
        ]
        self.histogram_e2e_time_request = \
            prometheus_client.Histogram(
                name="vllm:e2e_request_latency_seconds",
                documentation="Histogram of e2e request latency in seconds.",
                buckets=request_latency_buckets,
                labelnames=labelnames).labels(*labelvalues)
        self.histogram_queue_time_request = \
            prometheus_client.Histogram(
                name="vllm:request_queue_time_seconds",
                documentation=
                "Histogram of time spent in WAITING phase for request.",
                buckets=request_latency_buckets,
                labelnames=labelnames).labels(*labelvalues)
        self.histogram_inference_time_request = \
            prometheus_client.Histogram(
                name="vllm:request_inference_time_seconds",
                documentation=
                "Histogram of time spent in RUNNING phase for request.",
                buckets=request_latency_buckets,
                labelnames=labelnames).labels(*labelvalues)
        self.histogram_prefill_time_request = \
            prometheus_client.Histogram(
                name="vllm:request_prefill_time_seconds",
                documentation=
                "Histogram of time spent in PREFILL phase for request.",
                buckets=request_latency_buckets,
                labelnames=labelnames).labels(*labelvalues)
        self.histogram_decode_time_request = \
            prometheus_client.Histogram(
                name="vllm:request_decode_time_seconds",
                documentation=
                "Histogram of time spent in DECODE phase for request.",
                buckets=request_latency_buckets,
                labelnames=labelnames).labels(*labelvalues)

        #
        # LoRA metrics
        #
        self.gauge_lora_info: Optional[prometheus_client.Gauge] = None
        if vllm_config.lora_config is not None:
            self.labelname_max_lora = "max_lora"
            self.labelname_waiting_lora_adapters = "waiting_lora_adapters"
            self.labelname_running_lora_adapters = "running_lora_adapters"
            self.max_lora = vllm_config.lora_config.max_loras
            self.gauge_lora_info = \
                prometheus_client.Gauge(
                    name="vllm:lora_requests_info",
                    documentation="Running stats on lora requests.",
                    labelnames=[
                        self.labelname_max_lora,
                        self.labelname_waiting_lora_adapters,
                        self.labelname_running_lora_adapters,
                    ])

        #
        # Speculative Decoding metrics
        # The acceptance rate can be calculated using a PromQL query:
        #
        #   rate(vllm:spec_decode_num_accepted_tokens_total[$interval]) /
        #   rate(vllm:spec_decode_num_draft_tokens_total[$interval])
        #
        self.counter_spec_decode_num_draft_tokens = \
            prometheus_client.Counter(
                name="vllm:spec_decode_num_draft_tokens_total",
                documentation="Number of draft tokens.",
                labelnames=labelnames).labels(*labelvalues)
        self.counter_spec_decode_num_accepted_tokens = \
            prometheus_client.Counter(
                name="vllm:spec_decode_num_accepted_tokens_total",
                documentation="Number of accepted tokens.",
                labelnames=labelnames).labels(*labelvalues)

        #
        # Cache config info metric
        #
        self.log_metrics_info("cache_config", vllm_config.cache_config)

    def log_metrics_info(self, type: str, config_obj: SupportsMetricsInfo):
        metrics_info = config_obj.metrics_info()

        name, documentation = None, None
        if type == "cache_config":
            name = "vllm:cache_config_info"
            documentation = "Information of the LLMEngine CacheConfig"
        assert name is not None, f"Unknown metrics info type {type}"

        # Info type metrics are syntactic sugar for a gauge permanently set to 1
        # Since prometheus multiprocessing mode does not support Info, emulate
        # info here with a gauge.
        info_gauge = prometheus_client.Gauge(
            name=name,
            documentation=documentation,
            labelnames=metrics_info.keys()).labels(**metrics_info)
        info_gauge.set(1)

    def record(self, scheduler_stats: SchedulerStats,
               iteration_stats: Optional[IterationStats]):
        """Log to prometheus."""
        self.gauge_scheduler_running.set(scheduler_stats.num_running_reqs)
        self.gauge_scheduler_waiting.set(scheduler_stats.num_waiting_reqs)

        self.gauge_gpu_cache_usage.set(scheduler_stats.gpu_cache_usage)

        self.counter_gpu_prefix_cache_queries.inc(
            scheduler_stats.prefix_cache_stats.queries)
        self.counter_gpu_prefix_cache_hits.inc(
            scheduler_stats.prefix_cache_stats.hits)

        if scheduler_stats.spec_decoding_stats is not None:
            self.counter_spec_decode_num_draft_tokens.inc(
                scheduler_stats.spec_decoding_stats.num_draft_tokens)
            self.counter_spec_decode_num_accepted_tokens.inc(
                scheduler_stats.spec_decoding_stats.num_accepted_tokens)

        if iteration_stats is None:
            return

        self.counter_num_preempted_reqs.inc(iteration_stats.num_preempted_reqs)
        self.counter_prompt_tokens.inc(iteration_stats.num_prompt_tokens)
        self.counter_generation_tokens.inc(
            iteration_stats.num_generation_tokens)
        self.histogram_iteration_tokens.observe(
            iteration_stats.num_prompt_tokens + \
            iteration_stats.num_generation_tokens)

        for max_gen_tokens in iteration_stats.max_num_generation_tokens_iter:
            self.histogram_max_num_generation_tokens_request.observe(
                max_gen_tokens)
        for n_param in iteration_stats.n_params_iter:
            self.histogram_n_request.observe(n_param)
        for ttft in iteration_stats.time_to_first_tokens_iter:
            self.histogram_time_to_first_token.observe(ttft)
        for tpot in iteration_stats.time_per_output_tokens_iter:
            self.histogram_time_per_output_token.observe(tpot)

        for finished_request in iteration_stats.finished_requests:
            self.counter_request_success[finished_request.finish_reason].inc()
            self.histogram_e2e_time_request.observe(
                finished_request.e2e_latency)
            self.histogram_queue_time_request.observe(
                finished_request.queued_time)
            self.histogram_prefill_time_request.observe(
                finished_request.prefill_time)
            self.histogram_inference_time_request.observe(
                finished_request.inference_time)
            self.histogram_decode_time_request.observe(
                finished_request.decode_time)
            self.histogram_num_prompt_tokens_request.observe(
                finished_request.num_prompt_tokens)
            self.histogram_num_generation_tokens_request.observe(
                finished_request.num_generation_tokens)
            self.histogram_max_tokens_request.observe(
                finished_request.max_tokens_param)

        if self.gauge_lora_info is not None:
            running_lora_adapters = \
                ",".join(iteration_stats.running_lora_adapters.keys())
            waiting_lora_adapters = \
                ",".join(iteration_stats.waiting_lora_adapters.keys())
            lora_info_labels = {
                self.labelname_running_lora_adapters: running_lora_adapters,
                self.labelname_waiting_lora_adapters: waiting_lora_adapters,
                self.labelname_max_lora: self.max_lora,
            }
            self.gauge_lora_info.labels(**lora_info_labels)\
                                .set_to_current_time()

    @staticmethod
    def _unregister_vllm_metrics():
        # Unregister any existing vLLM collectors (for CI/CD
        for collector in list(prometheus_client.REGISTRY._collector_to_names):
            if hasattr(collector, "_name") and "vllm" in collector._name:
                prometheus_client.REGISTRY.unregister(collector)


def build_buckets(mantissa_lst: list[int], max_value: int) -> list[int]:
    """
    Builds a list of buckets with increasing powers of 10 multiplied by
    mantissa values until the value exceeds the specified maximum.

    """
    exponent = 0
    buckets: list[int] = []
    while True:
        for m in mantissa_lst:
            value = m * 10**exponent
            if value <= max_value:
                buckets.append(value)
            else:
                return buckets
        exponent += 1


def build_1_2_5_buckets(max_value: int) -> list[int]:
    """
    Example:
    >>> build_1_2_5_buckets(100)
    [1, 2, 5, 10, 20, 50, 100]
    """
    return build_buckets([1, 2, 5], max_value)


def build_cudagraph_buckets(vllm_config: VllmConfig) -> list[int]:
    if not vllm_config.model_config.enforce_eager:
        buckets = vllm_config.compilation_config.\
            cudagraph_capture_sizes.copy()
        buckets.sort()
        return buckets
    else:
        return [1, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8096]
