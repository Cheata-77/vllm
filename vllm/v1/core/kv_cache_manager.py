# SPDX-License-Identifier: Apache-2.0

from collections import defaultdict
from collections.abc import Iterable
from typing import Optional

from vllm.logger import init_logger
from vllm.utils import cdiv, sha256
from vllm.v1.core.block_pool import BlockPool
from vllm.v1.core.kv_cache_utils import (BlockHashType, KVCacheBlock,
                                         hash_request_tokens)
from vllm.v1.core.specialized_manager import get_specialized_manager
from vllm.v1.kv_cache_interface import KVCacheConfig
from vllm.v1.metrics.stats import PrefixCacheStats
from vllm.v1.request import Request, RequestStatus
from vllm.v1.metrics.loggers import CacheTelemetry

logger = init_logger(__name__)


class KVCacheManager:

    def __init__(
        self,
        kv_cache_config: KVCacheConfig,
        max_model_len: int,
        enable_caching: bool = True,
        caching_hash_algo: str = "builtin",
        num_preallocate_tokens: int = 64,
        log_stats: bool = False,
        cache_telemetry: CacheTelemetry = None,
    ) -> None:
        assert len(kv_cache_config.kv_cache_groups) == 1, (
            "KVCacheManager does not support hybrid models with more than 1 "
            "kv cache group")
        kv_cache_spec = kv_cache_config.kv_cache_groups[0].kv_cache_spec
        self.block_size = kv_cache_spec.block_size
        self.num_gpu_blocks = kv_cache_config.num_blocks
        self.max_model_len = max_model_len
        self.max_num_blocks_per_req = cdiv(max_model_len, self.block_size)

        self.enable_caching = enable_caching
        self.caching_hash_fn = sha256 if caching_hash_algo == "sha256" else hash
        # FIXME: make prefix cache stats conditional on log_stats
        self.log_stats = log_stats
        # NOTE(woosuk): To avoid frequent block allocation, we preallocate some
        # blocks for each request. For example, when a request reaches the end
        # of its block table, we preallocate N blocks in advance. This way, we
        # reduce the overhead of updating free_block_ids and ref_cnts for each
        # request every step (at the cost of some memory waste).
        # NOTE(woosuk): This is different from the "lookahead" slots since this
        # does not guarantee that the request always has N empty blocks. After
        # the request gets N empty blocks, it starts to use the blocks without
        # further allocation. When it uses up all the N empty blocks, it gets
        # N new empty blocks.
        self.num_preallocate_tokens = num_preallocate_tokens
        self.num_preallocate_blocks = cdiv(num_preallocate_tokens,
                                           self.block_size)

        self.block_pool = BlockPool(self.num_gpu_blocks, enable_caching)

        self.specialized_manager = get_specialized_manager(
            kv_cache_spec=kv_cache_spec,
            block_pool=self.block_pool,
        )
        
        self.cache_telemetry = cache_telemetry

        # Mapping from request ID to blocks to track the blocks allocated
        # for each request, so that we can free the blocks when the request
        # is finished.
        self.req_to_blocks: defaultdict[str,
                                        list[KVCacheBlock]] = defaultdict(list)

        # Mapping from request ID to kv block hashes.
        # This is to avoid recomputing the block hashes for each call of
        # `get_computed_blocks` or `allocate_slots`.
        self.req_to_block_hashes: defaultdict[
            str, list[BlockHashType]] = defaultdict(list)

        # {req_id: The number of cached blocks for this given request}
        # This is used to track the number of cached blocks for each request.
        # This is only used to track the RUNNING requests, we do not track the
        # data for reempted ones.
        self.num_cached_block: dict[str, int] = {}
        self.prefix_cache_stats = PrefixCacheStats()

    @property
    def usage(self) -> float:
        """Get the KV cache usage.

        Returns:
            The KV cache usage (between 0.0 and 1.0).
        """
        return self.block_pool.get_usage()

    def make_prefix_cache_stats(self) -> PrefixCacheStats:
        """Get (and reset) the prefix cache stats.

        Returns:
            The current prefix caching stats.
        """
        stats = self.prefix_cache_stats
        self.prefix_cache_stats = PrefixCacheStats()
        return stats

    def get_computed_blocks(
            self, request: Request) -> tuple[list[KVCacheBlock], int]:
        """Get the computed (cached) blocks for the request.
        Note that the computed blocks must be full.

        Args:
            request: The request to get the computed blocks.

        Returns:
            A tuple containing:
                - A list of blocks that are computed for the request.
                - The number of computed tokens.
        """
        
        if not self.enable_caching:
            # Prefix caching is disabled.
            return [], 0

        # The block hashes for the request may already be computed
        # if the scheduler has tried to schedule the request before.
        block_hashes = self.req_to_block_hashes[request.request_id]
        if not block_hashes:
            block_hashes = hash_request_tokens(self.caching_hash_fn,
                                               self.block_size, request)
            self.req_to_block_hashes[request.request_id] = block_hashes

        self.prefix_cache_stats.requests += 1
        # When the request requires prompt logprobs, we skip prefix caching.
        if request.sampling_params.prompt_logprobs is not None:
            return [], 0

        if len(block_hashes) * self.block_size == request.num_tokens:
            # When prompt length is divisible by the block size and all
            # blocks are cached, we need to recompute the last token. This
            # have to be achieved by re-computing an entire block because
            # allocate_slots() assumes num_computed_tokens is always a
            # multiple of the block size. To achieve this, remove the last
            # block hash from the block_hashes for find_longest_cache_hit
            # This limitation can potentially be removed in the future to
            # slightly improve the performance.
            last_block_hash = block_hashes.pop()
        else:
            last_block_hash = None

        computed_blocks = (
            self.specialized_manager.find_longest_cache_hit(block_hashes))
        
        self.prefix_cache_stats.queries += len(block_hashes)
        self.prefix_cache_stats.hits += len(computed_blocks)
        
        if len(computed_blocks) > 0:
            self.prefix_cache_stats.request_hits += 1
            
        if self.log_stats:
            self.cache_telemetry.record_hit(len(computed_blocks), request.request_id)
            self.cache_telemetry.record_miss(len(block_hashes) - len(computed_blocks), request.request_id)

        if last_block_hash is not None:
            # Add back the last block hash if it was removed.
            # NOTE: Because block_hashes is cached in req_to_block_hashes,
            # we shouldn't modify it directly.
            block_hashes.append(last_block_hash)

        # NOTE(woosuk): Since incomplete blocks are not eligible for
        # sharing, `num_computed_tokens` is always a multiple of
        # `block_size`.
        num_computed_tokens = len(computed_blocks) * self.block_size
        return computed_blocks, num_computed_tokens

    def allocate_slots(
        self,
        request: Request,
        num_tokens: int,
        new_computed_blocks: Optional[list[KVCacheBlock]] = None
    ) -> Optional[list[KVCacheBlock]]:
        """Add slots for a request with new tokens to append.

        Args:
            request: The request to allocate slots.
            num_tokens: The number of tokens to allocate. Note that this does
                not include the tokens that have already been computed.
            new_computed_blocks: A list of new computed blocks just hitting the
                prefix caching.

        Blocks layout:
        -----------------------------------------------------------------------
        | < computed > | < new computed > |    < new >    | < pre-allocated > |
        -----------------------------------------------------------------------
        |                  < required >                   |
        --------------------------------------------------
        |                    < full >                  |
        ------------------------------------------------
                                          | <new full> |
                                          --------------
        The following *_blocks are illustrated in this layout.

        Returns:
            A list of new allocated blocks.
        """
        if num_tokens == 0:
            raise ValueError("num_tokens must be greater than 0")

        new_computed_blocks = new_computed_blocks or []

        req_blocks = self.req_to_blocks[request.request_id]

        # Free the blocks that are skipped during the attention computation
        # (e.g., tokens outside the sliding window).
        # We can do this even if we cannot schedule this request due to
        # insufficient free blocks.
        # Should call this function before allocating new blocks to reduce
        # the number of evicted blocks.
        removed_blocks = self.specialized_manager.remove_skipped_blocks(
            req_blocks, request.num_computed_tokens)
        self.block_pool.free_blocks(removed_blocks)

        # The number of computed tokens is the number of computed tokens plus
        # the new prefix caching hits
        num_computed_tokens = (request.num_computed_tokens +
                               len(new_computed_blocks) * self.block_size)
        num_required_blocks = cdiv(num_computed_tokens + num_tokens,
                                   self.block_size)
        num_new_blocks = (num_required_blocks - len(req_blocks) -
                          len(new_computed_blocks))

        # If a computed block of a request is an eviction candidate (in the
        # free queue and ref_cnt == 0), it cannot be counted as a free block
        # when allocating this request.
        num_evictable_computed_blocks = sum(1 for blk in new_computed_blocks
                                            if blk.ref_cnt == 0)
        if (num_new_blocks > self.block_pool.get_num_free_blocks() -
                num_evictable_computed_blocks):
            # Cannot allocate new blocks
            return None

        # Touch the computed blocks to make sure they won't be evicted.
        if self.enable_caching:
            self.block_pool.touch(new_computed_blocks)
        else:
            assert not new_computed_blocks, (
                "Computed blocks should be empty when "
                "prefix caching is disabled")

        # Append the new computed blocks to the request blocks until now to
        # avoid the case where the new blocks cannot be allocated.
        req_blocks.extend(new_computed_blocks)

        # Start to handle new blocks

        if num_new_blocks <= 0:
            # No new block is needed.
            new_blocks = []
        else:
            # Get new blocks from the free block pool considering
            # preallocated blocks.
            num_new_blocks = min(
                num_new_blocks + self.num_preallocate_blocks,
                self.block_pool.get_num_free_blocks(),
                # Should not exceed the maximum number of blocks per request.
                # This is especially because the block table has the shape
                # [..., max_num_blocks_per_req].
                self.max_num_blocks_per_req - len(req_blocks),
            )
            assert num_new_blocks > 0
            num_readily_free_blocks = self.block_pool.get_num_free_blocks()
            
            if(num_new_blocks > num_readily_free_blocks):
                num_blocks_to_evict = num_new_blocks - num_readily_free_blocks
                self.prefix_cache_stats.evictions += num_blocks_to_evict
                self.prefix_cache_stats.request_evictions += 1
                
                if self.log_stats:
                    self.cache_telemetry.record_eviction(num_blocks_to_evict, request.request_id)
                
            # Concatenate the computed block IDs and the new block IDs.
            new_blocks = self.block_pool.get_new_blocks(num_new_blocks)
            req_blocks.extend(new_blocks)

        if not self.enable_caching:
            return new_blocks

        # Use `new_computed_blocks` for a new request, and `num_cached_block`
        # for a running request.
        num_cached_blocks = self.num_cached_block.get(request.request_id,
                                                      len(new_computed_blocks))
        # Speculated tokens might be rejected in the future, so we does
        # not cache any speculated tokens. We only cache blocks with
        # generated (accepted) tokens.
        num_full_blocks_after_append = (num_computed_tokens + num_tokens - len(
            request.spec_token_ids)) // self.block_size

        self.block_pool.cache_full_blocks(
            request=request,
            blocks=req_blocks,
            block_hashes=self.req_to_block_hashes[request.request_id],
            num_cached_blocks=num_cached_blocks,
            num_full_blocks=num_full_blocks_after_append,
            block_size=self.block_size,
            hash_fn=self.caching_hash_fn,
        )

        self.num_cached_block[
            request.request_id] = num_full_blocks_after_append
        return new_blocks

    def free(self, request: Request) -> None:
        """Free the blocks allocated for the request.
        When caching is enabled, we free the blocks in reverse order so that
        the tail blocks are evicted first.

        Args:
            request: The request to free the blocks.
        """
        # Default to [] in case a request is freed (aborted) before alloc.
        blocks = self.req_to_blocks.pop(request.request_id, [])
        ordered_blocks: Iterable[KVCacheBlock] = blocks
        if self.enable_caching:
            # Free blocks in reverse order so that the tail blocks are
            # freed first.
            ordered_blocks = reversed(blocks)

        self.block_pool.free_blocks(ordered_blocks)
        self.num_cached_block.pop(request.request_id, None)

    def reset_prefix_cache(self) -> bool:
        """Reset prefix cache. This function may be used in RLHF
        flows to invalid prefix caching after the weights are updated,
        or used for resetting prefix caching status for benchmarking.

        Returns:
            bool: True if the prefix cache is successfully reset,
            False otherwise.
        """
        if self.block_pool.reset_prefix_cache():
            self.prefix_cache_stats.reset = True
            return True
        return False

    def get_num_common_prefix_blocks(
        self,
        request: Request,
        num_running_requests: int,
    ) -> int:
        """Calculate the number of common prefix blocks shared by all requests
        in the RUNNING state.

        The function determines this by selecting any request and iterating
        through its blocks.  A block is considered a common prefix block if its
        `ref_cnt` equals the total number of requests in the RUNNING state.

        NOTE(woosuk): The number of requests in the RUNNING state is **greater
        than or equal to** the number of requests scheduled in the current step.
        This is because the RUNNING state only indicates that:
        1. The request has not yet finished, and
        2. The request holds its blocks unfreed.

        While all scheduled requests must be in the RUNNING state, the inverse
        is not necessarily true. There may be RUNNING requests that are not
        scheduled in the current step.

        This can result in an edge case where the number of common prefix blocks
        is 0, even though all scheduled requests share a common prefix. This
        occurs because there may be unscheduled RUNNING requests that do not
        share the common prefix. Currently, this case cannot be easily detected,
        so the function returns 0 in such cases.

        Args:
            request: Any request in the RUNNING state, used to identify the
                common prefix blocks.
            num_running_requests: The total number of requests in the RUNNING
                state. This can be different from the number of scheduled
                requests in the current step.

        Returns:
            int: The number of common prefix blocks.
        """
        assert request.status == RequestStatus.RUNNING
        blocks = self.req_to_blocks[request.request_id]
        num_common_blocks = 0
        for block in blocks:
            if block.ref_cnt == num_running_requests:
                num_common_blocks += 1
            else:
                break
        return num_common_blocks

    def free_block_hashes(self, request: Request) -> None:
        """Discard the block hashes for the request.

        NOTE: Unlike `free`, this method should be called only when the request
        is finished, not when it is preempted.
        """
        self.req_to_block_hashes.pop(request.request_id, None)
