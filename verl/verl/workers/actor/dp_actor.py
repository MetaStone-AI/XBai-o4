# Copyright 2024 Bytedance Ltd. and/or its affiliates
# Copyright 2023-2024 SGLang Team
# Copyright 2025 ModelBest Inc. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Single Process Actor
"""

import itertools
import logging
import os
from typing import Tuple

import torch
from flash_attn.bert_padding import index_first_axis, pad_input, rearrange, unpad_input
from torch import nn
import torch.nn.functional as F
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP

import verl.utils.torch_functional as verl_F
from verl import DataProto
from verl.trainer.ppo.core_algos import agg_loss, compute_policy_loss, kl_penalty
from verl.utils.debug import GPUMemoryLogger
from verl.utils.fsdp_utils import FSDPModule, fsdp2_clip_grad_norm_
from verl.utils.py_functional import append_to_dict
from verl.utils.seqlen_balancing import get_reverse_idx, rearrange_micro_batches
from verl.utils.torch_functional import logprobs_from_logits
from verl.utils.ulysses import gather_outpus_and_unpad, ulysses_pad_and_slice_inputs
from verl.workers.actor import BasePPOActor
import torch.distributed as dist
# from megatron.core import mpu
import os

__all__ = ["DataParallelPPOActor"]

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


def topk_percent_mask(x, correct_mask, *args, **kwargs):
    mask = torch.zeros_like(x, dtype=torch.bool)  # 初始化全0的mask
    mask[(x>0) & correct_mask] = 1
    mask[(x<0) & (~correct_mask)] = 1
    return mask

def geommean_scores(x: torch.Tensor, eps: float = 1e-8) -> torch.Tensor:
    # 非零 mask
    mask = (x != 0).float()
    masked_x = torch.where(mask.bool(), torch.sigmoid(x), torch.ones_like(x))  # 0 元素替换为 1，不影响乘积

    # 计算几何平均（按行）
    # 几何平均 = exp(sum(log(x_i)) / n)，只对非零项
    log_x = torch.log(masked_x + eps) * mask  # 只对非零项取 log
    count = mask.sum(dim=1) + eps  # 避免除以 0
    geom_mean = torch.exp(log_x.sum(dim=1) / count)  # shape: (b,)

    # 全 0 行设为 0
    all_zero_mask = mask.sum(dim=1) == 0
    geom_mean[all_zero_mask] = 0.0

    return geom_mean  # shape: (b,)

class DataParallelPPOActor(BasePPOActor):

    def __init__(
        self,
        config,
        actor_module: nn.Module,
        actor_optimizer: torch.optim.Optimizer = None,
        score_module = None,
        score_optimizer = None,
        tokenizer = None,
    ):
        """When optimizer is None, it is Reference Policy"""
        super().__init__(config)
        self.actor_module = actor_module
        self.actor_optimizer = actor_optimizer

        self.use_remove_padding = self.config.get("use_remove_padding", False)
        print(f"Actor use_remove_padding={self.use_remove_padding}")
        self.use_fused_kernels = self.config.get("use_fused_kernels", False)
        print(f"Actor use_fused_kernels={self.use_fused_kernels}")

        self.ulysses_sequence_parallel_size = self.config.ulysses_sequence_parallel_size
        self.use_ulysses_sp = self.ulysses_sequence_parallel_size > 1

        self.compute_entropy_from_logits = (
            torch.compile(verl_F.entropy_from_logits, dynamic=True)
            if self.config.get("use_torch_compile", True)  #  use torch compile by default
            else verl_F.entropy_from_logits
        )

        if self.use_fused_kernels:
            from verl.utils.experimental.torch_functional import FusedLinearForPPO

            self.fused_linear_for_ppo = FusedLinearForPPO()

            # FusedLinearForPPO has an error when compiled, disable for now
            # if self.config.get("use_torch_compile", True):
            #     self.fused_linear_for_ppo.compile(dynamic=True)
        self.use_score = score_module is not None
        if self.use_score:
            self.score_module = score_module
            self.score_optimizer = score_optimizer
            self.tokenizer = tokenizer
            self.score_id = self.tokenizer.encode('.\n\n', add_special_tokens=False)[0]

    def _forward_micro_batch(self, micro_batch, temperature, calculate_entropy=False) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns:
            entropy: # (bs, response_len)
            log_probs: # (bs, response_len)
        """

        response_length = micro_batch["responses"].size(-1)
        multi_modal_inputs = {}
        if "multi_modal_inputs" in micro_batch:
            for key in micro_batch["multi_modal_inputs"][0].keys():
                multi_modal_inputs[key] = torch.cat([inputs[key] for inputs in micro_batch["multi_modal_inputs"]], dim=0)

        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            input_ids = micro_batch["input_ids"]
            batch_size, seqlen = input_ids.shape
            attention_mask = micro_batch["attention_mask"]
            position_ids = micro_batch["position_ids"]
            entropy = None
            if position_ids.dim() == 3:  # qwen2vl mrope
                position_ids = position_ids.transpose(0, 1)  # (bsz, 3, seqlen) -> (3, bsz, seqlen)

            if self.use_remove_padding:
                input_ids_rmpad, indices, *_ = unpad_input(input_ids.unsqueeze(-1), attention_mask)  # input_ids_rmpad (total_nnz, ...)
                input_ids_rmpad = input_ids_rmpad.transpose(0, 1)  # (1, total_nnz)

                # unpad the position_ids to align the rotary
                if position_ids.dim() == 3:
                    position_ids_rmpad = index_first_axis(rearrange(position_ids, "c b s ... -> (b s) c ..."), indices).transpose(0, 1).unsqueeze(1)  # (3, bsz, seqlen) -> (3, 1, bsz * seqlen)
                else:
                    position_ids_rmpad = index_first_axis(rearrange(position_ids.unsqueeze(-1), "b s ... -> (b s) ..."), indices).transpose(0, 1)

                # for compute the log_prob
                input_ids_rmpad_rolled = torch.roll(input_ids_rmpad, shifts=-1, dims=1)  # (1, total_nnz)

                # pad and slice the inputs if sp > 1
                if self.use_ulysses_sp:
                    input_ids_rmpad, position_ids_rmpad, pad_size = ulysses_pad_and_slice_inputs(
                        input_ids_rmpad,
                        position_ids_rmpad=position_ids_rmpad,
                        sp_size=self.ulysses_sequence_parallel_size,
                    )
                    input_ids_rmpad_rolled, _, _ = ulysses_pad_and_slice_inputs(
                        input_ids_rmpad_rolled,
                        position_ids_rmpad=None,
                        sp_size=self.ulysses_sequence_parallel_size,
                    )

                input_ids_rmpad_rolled = input_ids_rmpad_rolled.squeeze(0)  # ((total_nnz / sp) + pad)

                # only pass input_ids and position_ids to enable flash_attn_varlen
                output = self.actor_module(
                    input_ids=input_ids_rmpad,
                    attention_mask=None,
                    position_ids=position_ids_rmpad,
                    **multi_modal_inputs,
                    use_cache=False,
                )  # prevent model thinks we are generating

                if self.use_fused_kernels:
                    hidden_states = output.last_hidden_state
                    vocab_weights = self.actor_module.lm_head.weight

                    log_probs, entropy_rmpad = self.fused_linear_for_ppo(
                        hidden_states=hidden_states.squeeze(0),
                        vocab_weights=vocab_weights,
                        input_ids=input_ids_rmpad_rolled,
                        temperature=temperature,
                    )

                else:
                    logits_rmpad = output.logits.squeeze(0)  # (total_nnz, vocab_size)

                    # logits_rmpad = output.logits.squeeze(0)  # (total_nnz, vocab_size)
                    logits_rmpad.div_(temperature)

                    # if use_sp: ((total_nnz / sp) + pad) ; if not use_sp: (batch, seqlen)
                    inplace_backward = True
                    if calculate_entropy:
                        inplace_backward = False
                    log_probs = logprobs_from_logits(
                        logits=logits_rmpad,
                        labels=input_ids_rmpad_rolled,
                        inplace_backward=inplace_backward,
                    )

                    # compute entropy
                    if calculate_entropy:
                        entropy_rmpad = self.compute_entropy_from_logits(logits_rmpad)  # ((total_nnz / sp) + pad)

                # gather log_prob if sp > 1
                if self.use_ulysses_sp:
                    # gather and unpad for the ulysses sp
                    log_probs = gather_outpus_and_unpad(
                        log_probs,
                        gather_dim=0,
                        unpad_dim=0,
                        padding_size=pad_size,
                    )
                    if calculate_entropy:
                        entropy_rmpad = gather_outpus_and_unpad(
                            entropy_rmpad,
                            gather_dim=0,
                            unpad_dim=0,
                            padding_size=pad_size,
                        )
                # pad back to (bsz, seqlen)
                if calculate_entropy:
                    full_entropy = pad_input(
                        hidden_states=entropy_rmpad.unsqueeze(-1),
                        indices=indices,
                        batch=batch_size,
                        seqlen=seqlen,
                    )
                full_log_probs = pad_input(
                    hidden_states=log_probs.unsqueeze(-1),
                    indices=indices,
                    batch=batch_size,
                    seqlen=seqlen,
                )

                # only return response part:
                if calculate_entropy:
                    entropy = full_entropy.squeeze(-1)[:, -response_length - 1 : -1]  # (bsz, response_length)
                log_probs = full_log_probs.squeeze(-1)[:, -response_length - 1 : -1]  # (bsz, response_length)

            else:  # not using rmpad and no ulysses sp
                output = self.actor_module(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    **multi_modal_inputs,
                    use_cache=False,
                )  # prevent model thinks we are generating

                if self.use_fused_kernels:
                    hidden_states = output.last_hidden_state
                    vocab_weights = self.actor_module.lm_head.weight

                    log_probs, entropy = self.fused_linear_for_ppo(
                        hidden_states=hidden_states[:, -response_length - 1 : -1, :],
                        vocab_weights=vocab_weights,
                        input_ids=micro_batch["responses"],
                        temperature=temperature,
                    )

                else:
                    logits = output.logits

                    logits.div_(temperature)
                    logits = logits[:, -response_length - 1 : -1, :]  # (bsz, response_length, vocab_size)
                    log_probs = logprobs_from_logits(logits, micro_batch["responses"])
                    if calculate_entropy:
                        entropy = verl_F.entropy_from_logits(logits)  # (bsz, response_length)

            return entropy, log_probs

    def _forward_micro_batch_with_score(self, micro_batch, temperature) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns:
            entropy: # (bs, response_len)
            log_probs: # (bs, response_len)
        """
        response_length = micro_batch['responses'].size(-1)
        multi_modal_inputs = {}
        if "multi_modal_inputs" in micro_batch:
            for key in micro_batch["multi_modal_inputs"][0].keys():
                multi_modal_inputs[key] = torch.cat([inputs[key] for inputs in micro_batch["multi_modal_inputs"]], dim=0)

        with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
            input_ids = micro_batch['input_ids']
            batch_size, seqlen = input_ids.shape
            attention_mask = micro_batch['attention_mask']
            position_ids = micro_batch['position_ids']
            advantages = micro_batch['advantages']
            reward = micro_batch['rollout_level_scores'].max(dim=1)[0]
            
            if position_ids.dim() == 3:  # qwen2vl mrope
                position_ids = position_ids.transpose(0, 1)  # (bsz, 3, seqlen) -> (3, bsz, seqlen)
                
            response_masks = torch.cat([torch.zeros_like(input_ids[:,:seqlen-response_length]),torch.ones_like(input_ids[:,:response_length])],dim=1)
            if self.use_remove_padding:
                input_ids_rmpad, indices, *_ = unpad_input(input_ids.unsqueeze(-1),
                                                            attention_mask)  # input_ids_rmpad (total_nnz, ...)
                input_ids_rmpad = input_ids_rmpad.transpose(0, 1)  # (1, total_nnz)

                response_masks_rmpad, *_ = unpad_input(response_masks.unsqueeze(-1), attention_mask)
                response_masks_rmpad = response_masks_rmpad.transpose(0, 1)

                score_mask_rmpad = input_ids_rmpad==self.score_id

                # unpad the position_ids to align the rotary
                if position_ids.dim() == 3:
                    position_ids_rmpad = index_first_axis(rearrange(position_ids, "c b s ... -> (b s) c ..."), indices).transpose(0, 1).unsqueeze(1)  # (3, bsz, seqlen) -> (3, 1, bsz * seqlen)
                else:
                    position_ids_rmpad = index_first_axis(rearrange(position_ids.unsqueeze(-1), "b s ... -> (b s) ..."), indices).transpose(0, 1)

                # for compute the log_prob
                input_ids_rmpad_rolled = torch.roll(input_ids_rmpad, shifts=-1, dims=1)  # (1, total_nnz)
                score_mask_rmpad_rolled = torch.roll(score_mask_rmpad, shifts=-1, dims=1)
                response_masks_rmpad_rolled = torch.roll(response_masks_rmpad, shifts=-1, dims=1)

                # pad and slice the inputs if sp > 1
                if self.use_ulysses_sp:
                    input_ids_rmpad, position_ids_rmpad, pad_size = ulysses_pad_and_slice_inputs(input_ids_rmpad, \
                                                                                                position_ids_rmpad, \
                                                                                                sp_size=self.ulysses_sequence_parallel_size)
                    input_ids_rmpad_rolled, _, _ = ulysses_pad_and_slice_inputs(input_ids_rmpad_rolled, None,
                                                                                self.ulysses_sequence_parallel_size)

                input_ids_rmpad_rolled = input_ids_rmpad_rolled.squeeze(0)  # ((total_nnz / sp) + pad)

                # only pass input_ids and position_ids to enable flash_attn_varlen
                output = self.actor_module(input_ids=input_ids_rmpad,
                                            attention_mask=None,
                                            position_ids=position_ids_rmpad,
                                            **multi_modal_inputs,
                                            use_cache=False,
                                            output_hidden_states=True)  # prevent model thinks we are generating
                logits_rmpad = output.logits.squeeze(0)  # (total_nnz, vocab_size)

                logits_rmpad.div_(temperature)

                # compute entropy
                entropy_rmpad = self.compute_entropy_from_logits(logits_rmpad)  # ((total_nnz / sp) + pad)

                # if use_sp: ((total_nnz / sp) + pad) ; if not use_sp: (batch, seqlen)
                log_probs = logprobs_from_logits(logits=logits_rmpad, labels=input_ids_rmpad_rolled, inplace_backward=False) # calculate entropy, fix 1

                output_features = output.hidden_states[-2][0]
                output_features = output_features*0.0001 + output_features.detach()*(1-0.0001)

                # gather log_prob if sp > 1
                if self.use_ulysses_sp:
                    # gather and unpad for the ulysses sp
                    log_probs = gather_outpus_and_unpad(log_probs, gather_dim=0, unpad_dim=0, padding_size=pad_size)
                    entropy_rmpad = gather_outpus_and_unpad(entropy_rmpad,
                                                            gather_dim=0,
                                                            unpad_dim=0,
                                                            padding_size=pad_size)
                    output_features = gather_outpus_and_unpad(output_features,
                                                            gather_dim=0,
                                                            unpad_dim=0,
                                                            padding_size=pad_size)
                    input_ids_rmpad_rolled = gather_outpus_and_unpad(input_ids_rmpad_rolled, gather_dim=0, unpad_dim=0, padding_size=pad_size)

                # print(input_ids_rmpad_rolled.shape, score_mask_rmpad_rolled.shape, response_masks_rmpad_rolled.shape)
                score_loss_mask = (score_mask_rmpad_rolled==1) & (response_masks_rmpad_rolled==1) #(1,L)
                assert score_loss_mask.shape[0]==1
                if score_loss_mask.any():
                    score_probs = torch.zeros_like(output_features[:,0]) #L
                    score_features = output_features[score_loss_mask[0]] #L,C
                    score_probs[score_loss_mask[0]] = self.score_module(score_features).squeeze(1) #L
                    score_probs = pad_input(hidden_states=score_probs.unsqueeze(-1),
                                            indices=indices,
                                            batch=batch_size,
                                            seqlen=seqlen)
                    assert score_probs.shape[1] >= response_length + 1, f"score_probs too short: {score_probs.shape}"
                    score_probs = score_probs.squeeze(2)[:, -response_length - 1:-1]
                    correct_mask = (reward.unsqueeze(1).expand((batch_size, response_length)) >0.5)
                    rank_mask = topk_percent_mask(score_probs, correct_mask)
                    criterion = nn.BCEWithLogitsLoss(reduction='none').to(score_probs.device)
                    score_loss = criterion(score_probs, correct_mask.float())
                    valid_mask = (reward.unsqueeze(1).expand((batch_size, response_length))!=0.1).float()
                    final_mask = valid_mask * rank_mask

                    # -------------------------------------- #
                    ## balance loss
                    pos_mask = final_mask * correct_mask.float()
                    neg_mask = final_mask * (1 - correct_mask.float())
                    pos_count = pos_mask.sum()
                    neg_count = neg_mask.sum()
                    dist.barrier()
                    rank = dist.get_rank()
                    # print(f"[RANK {rank}] entered", flush=True)
                    dist.all_reduce(pos_count, op=dist.ReduceOp.SUM)
                    dist.all_reduce(neg_count, op=dist.ReduceOp.SUM)
                    total = pos_count + neg_count + 1e-5
                    neg_weight = pos_count / total
                    pos_weight = 1.5 * neg_count / total

                    weights = pos_mask * pos_weight + neg_mask * neg_weight
                    score_loss_mean = (score_loss * weights).sum() / (weights.sum() + 1e-5)
                    # -------------------------------------- #

                    with torch.no_grad():
                        mean_prob = geommean_scores(score_probs*valid_mask)
                        # print(mean_prob.detach().tolist(), reward.detach().tolist())
                        correct_prob = mean_prob[reward>0.5].sum().detach().item()
                        correct_num = (reward>0.5).sum().detach().item()
                        false_prob = mean_prob[reward==0].sum().detach().item()
                        false_num = (reward==0).sum().detach().item()
                else:
                    score_features = output_features[:1]
                    score_probs = self.score_module(score_features)
                    score_loss_mean = score_probs.sum() * 0.0
                    correct_prob, correct_num, false_prob, false_num = 0,0,0,0

                # pad back to (bsz, seqlen)
                full_entropy = pad_input(hidden_states=entropy_rmpad.unsqueeze(-1),
                                        indices=indices,
                                        batch=batch_size,
                                        seqlen=seqlen)
                full_log_probs = pad_input(hidden_states=log_probs.unsqueeze(-1),
                                        indices=indices,
                                        batch=batch_size,
                                        seqlen=seqlen)

                # only return response part:
                entropy = full_entropy.squeeze(-1)[:, -response_length - 1:-1]  # (bsz, response_length)
                log_probs = full_log_probs.squeeze(-1)[:, -response_length - 1:-1]  # (bsz, response_length)

            else:  # not using rmpad and no ulysses sp
                output = self.actor_module(input_ids=input_ids,
                                        attention_mask=attention_mask,
                                        position_ids=position_ids,
                                        use_cache=False)  # prevent model thinks we are generating
                logits = output.logits
                logits.div_(temperature)
                logits = logits[:, -response_length - 1:-1, :]  # (bsz, response_length, vocab_size)
                log_probs = logprobs_from_logits(logits, micro_batch['responses'])
                entropy = verl_F.entropy_from_logits(logits)  # (bsz, response_length)

            return entropy, log_probs, score_loss_mean, (correct_prob, correct_num, false_prob, false_num)

    def _optimizer_step(self):
        assert self.config.grad_clip is not None

        if isinstance(self.actor_module, FSDP):
            grad_norm = self.actor_module.clip_grad_norm_(max_norm=self.config.grad_clip)
        elif isinstance(self.actor_module, FSDPModule):
            grad_norm = fsdp2_clip_grad_norm_(self.actor_module.parameters(), max_norm=self.config.grad_clip)
        else:
            grad_norm = torch.nn.utils.clip_grad_norm_(self.actor_module.parameters(), max_norm=self.config.grad_clip)

        # if grad_norm is not finite, skip the update
        if not torch.isfinite(grad_norm):
            print(f"WARN: rank {torch.distributed.get_rank()} grad_norm is not finite: {grad_norm}")
            self.actor_optimizer.zero_grad()
        else:
            self.actor_optimizer.step()
        if self.use_score:
            if isinstance(self.score_module, FSDP):
                grad_norm_fc = self.score_module.clip_grad_norm_(max_norm=self.config.grad_clip)
            elif isinstance(self.score_module, FSDPModule):
                grad_norm_fc = fsdp2_clip_grad_norm_(self.score_module.parameters(), max_norm=self.config.grad_clip)
            else:
                grad_norm_fc = torch.nn.utils.clip_grad_norm_(self.score_module.parameters(), max_norm=self.config.grad_clip)
            self.score_optimizer.step()
            # print([x.grad for x in self.score_optimizer.param_groups[0]['params']])
            # print(next(self.score_module.parameters()))
        else:
            grad_norm_fc = None
        return grad_norm, grad_norm_fc

    @GPUMemoryLogger(role="dp actor", logger=logger)
    def compute_log_prob(self, data: DataProto, calculate_entropy=False) -> torch.Tensor:
        """Compute the log probability of the responses given input_ids, attention_mask and position_ids

        Args:
            data (DataProto): a DataProto containing keys

                ``input_ids``: tensor of shape [batch_size, sequence_length]. torch.int64. Note that input_ids is the
                concatenation of prompt and response. Note that ``sequence_length = prompt_length + response_length``.

                ``attention_mask``: tensor of shape [batch_size, sequence_length]. torch.int64.

                ``position_ids``: tensor of shape [batch_size, sequence_length]. torch.int64.

                ``responses``:  tensor of shape [batch_size, response_length]. torch.int64.

        Returns:
            torch.Tensor: the log_prob tensor
        """
        # set to eval
        self.actor_module.eval()

        micro_batch_size = data.meta_info["micro_batch_size"]
        temperature = data.meta_info["temperature"]  # temperature must be in the data.meta_info to avoid silent error
        use_dynamic_bsz = data.meta_info["use_dynamic_bsz"]

        select_keys = ["responses", "input_ids", "attention_mask", "position_ids"]
        batch = data.select(batch_keys=select_keys).batch
        has_multi_modal_inputs = "multi_modal_inputs" in data.non_tensor_batch.keys()

        if has_multi_modal_inputs:
            num_micro_batches = data.batch.batch_size[0] // micro_batch_size
            non_tensor_select_keys = ["multi_modal_inputs"]
            micro_batches = data.select(select_keys, non_tensor_select_keys).chunk(num_micro_batches)
        elif use_dynamic_bsz:
            # split using dynamic bsz
            max_token_len = data.meta_info["max_token_len"] * self.ulysses_sequence_parallel_size
            micro_batches, indices = rearrange_micro_batches(batch=batch, max_token_len=max_token_len)
        else:
            micro_batches = batch.split(micro_batch_size)

        log_probs_lst = []
        entropy_lst = []
        for micro_batch in micro_batches:
            if isinstance(micro_batch, DataProto):
                micro_batch = {**micro_batch.batch, **micro_batch.non_tensor_batch}
            with torch.no_grad():
                entropy, log_probs = self._forward_micro_batch(micro_batch, temperature=temperature, calculate_entropy=calculate_entropy)
            log_probs_lst.append(log_probs)
            if calculate_entropy:
                entropy_lst.append(entropy)

        log_probs = torch.concat(log_probs_lst, dim=0)
        entropys = None
        if calculate_entropy:
            entropys = torch.concat(entropy_lst, dim=0)
        if use_dynamic_bsz:
            indices = list(itertools.chain.from_iterable(indices))
            assert len(indices) == log_probs.size(0), f"{len(indices)} vs. {log_probs.size()}"
            revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
            log_probs = log_probs[revert_indices]

        return log_probs, entropys

    @GPUMemoryLogger(role="dp actor", logger=logger)
    def update_policy(self, data: DataProto):
        # make sure we are in training mode
        self.actor_module.train()

        temperature = data.meta_info["temperature"]  # temperature must be in the data.meta_info to avoid silent error
        multi_turn = data.meta_info.get("multi_turn", False)

        select_keys = ['responses', 'input_ids', 'attention_mask', 'position_ids', 'old_log_probs', 'advantages', 'rollout_level_scores']

        if multi_turn:
            select_keys.append("loss_mask")
            
        if self.config.use_kl_loss:
            select_keys.append("ref_log_prob")
        batch = data.select(batch_keys=select_keys).batch
        has_multi_modal_inputs = "multi_modal_inputs" in data.non_tensor_batch.keys()

        # Split to make minibatch iterator for updating the actor
        # See PPO paper for details. https://arxiv.org/abs/1707.06347
        if has_multi_modal_inputs:
            num_mini_batches = data.batch.batch_size[0] // self.config.ppo_mini_batch_size
            non_tensor_select_keys = ["multi_modal_inputs"]
            dataloader = data.select(select_keys, non_tensor_select_keys).chunk(num_mini_batches)
        else:
            dataloader = batch.split(self.config.ppo_mini_batch_size)

        metrics = {}
        for epoch in range(self.config.ppo_epochs):
            for batch_idx, data in enumerate(dataloader):
                # split batch into micro_batches
                mini_batch = data
                if has_multi_modal_inputs:
                    self.gradient_accumulation = self.config.ppo_mini_batch_size // self.config.ppo_micro_batch_size_per_gpu
                    num_micro_batches = mini_batch.batch.batch_size[0] // self.config.ppo_micro_batch_size_per_gpu
                    micro_batches = data.select(select_keys, non_tensor_select_keys).chunk(num_micro_batches)
                elif self.config.use_dynamic_bsz:
                    max_token_len = self.config.ppo_max_token_len_per_gpu * self.ulysses_sequence_parallel_size
                    micro_batches, _ = rearrange_micro_batches(batch=mini_batch, max_token_len=max_token_len)
                else:
                    self.gradient_accumulation = self.config.ppo_mini_batch_size // self.config.ppo_micro_batch_size_per_gpu
                    # split batch into micro_batches
                    micro_batches = mini_batch.split(self.config.ppo_micro_batch_size_per_gpu)

                self.actor_optimizer.zero_grad()
                self.score_optimizer.zero_grad()
                print(f"micro_batches: {len(micro_batches)}", flush=True) #2
                correct_prob, correct_num, false_prob, false_num = 0,0,0,0
                for data in micro_batches:
                    # Support all hardwares
                    if isinstance(data, DataProto):
                        data = {**data.batch.to(torch.cuda.current_device()), **data.non_tensor_batch}
                    else:
                        data = data.to(torch.cuda.current_device())  # actor device is cpu when using offload
                    responses = data["responses"]
                    response_length = responses.size(1)
                    attention_mask = data["attention_mask"]
                    if multi_turn:
                        response_mask = data["loss_mask"][:, -response_length:]
                    else:
                        response_mask = attention_mask[:, -response_length:]

                    old_log_prob = data["old_log_probs"]
                    advantages = data["advantages"]

                    clip_ratio = self.config.clip_ratio
                    clip_ratio_low = self.config.clip_ratio_low if self.config.clip_ratio_low is not None else clip_ratio
                    clip_ratio_high = self.config.clip_ratio_high if self.config.clip_ratio_high is not None else clip_ratio
                    clip_ratio_c = self.config.get("clip_ratio_c", 3.0)
                    entropy_coeff = self.config.entropy_coeff
                    loss_agg_mode = self.config.loss_agg_mode

                    # all return: (bsz, response_length)
                    calculate_entropy = False
                    if entropy_coeff != 0:
                        calculate_entropy = True
                    # all return: (bsz, response_length)
                    policy_loss = 0
                    if self.use_score:
                        entropy, log_prob, score_loss, score_metrics = self._forward_micro_batch_with_score(micro_batch=data, temperature=temperature)
                        # rank = dist.get_rank()
                        # print(f"{rank} cal loss...: {score_loss}", flush=True)
                        policy_loss += score_loss
                        correct_prob += score_metrics[0]
                        correct_num += score_metrics[1]
                        # print(f'correct_num: {correct_num},false_num:{false_num}')
                        false_prob += score_metrics[2]
                        false_num += score_metrics[3]
                        metrics['fc/score_loss'] = score_loss.detach().item()
                    else:
                        entropy, log_prob = self._forward_micro_batch(micro_batch=data, temperature=temperature, calculate_entropy=calculate_entropy)

                    pg_loss, pg_clipfrac, ppo_kl, pg_clipfrac_lower = compute_policy_loss(
                        old_log_prob=old_log_prob,
                        log_prob=log_prob,
                        advantages=advantages,
                        response_mask=response_mask,
                        cliprange=clip_ratio,
                        cliprange_low=clip_ratio_low,
                        cliprange_high=clip_ratio_high,
                        clip_ratio_c=clip_ratio_c,
                        loss_agg_mode=loss_agg_mode,
                    )

                    if entropy_coeff != 0:
                        entropy_loss = agg_loss(loss_mat=entropy, loss_mask=response_mask, loss_agg_mode=loss_agg_mode)

                        # compute policy loss
                        policy_loss += pg_loss - entropy_loss * entropy_coeff
                    else:
                        policy_loss += pg_loss

                    if self.config.use_kl_loss:
                        ref_log_prob = data["ref_log_prob"]
                        # compute kl loss
                        kld = kl_penalty(logprob=log_prob, ref_logprob=ref_log_prob, kl_penalty=self.config.kl_loss_type)
                        kl_loss = agg_loss(loss_mat=kld, loss_mask=response_mask, loss_agg_mode=self.config.loss_agg_mode)

                        policy_loss = policy_loss + kl_loss * self.config.kl_loss_coef
                        metrics["actor/kl_loss"] = kl_loss.detach().item()
                        metrics["actor/kl_coef"] = self.config.kl_loss_coef

                    if self.config.use_dynamic_bsz:
                        # relative to the dynamic bsz
                        loss = policy_loss * (len(data) / self.config.ppo_mini_batch_size)
                    else:
                        loss = policy_loss / self.gradient_accumulation
                    loss.backward()

                    data = {
                        'actor/entropy_loss': entropy_loss.detach().item(),
                        "actor/pg_loss": pg_loss.detach().item(),
                        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
                        "actor/ppo_kl": ppo_kl.detach().item(),
                        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
                    }
                    append_to_dict(metrics, data)

                grad_norm, grad_norm_fc = self._optimizer_step()
                data = {'actor/grad_norm': grad_norm.detach().item()}
                append_to_dict(metrics, data)
                if self.use_score:
                    data = {
                        'fc/grad_norm': grad_norm_fc.detach().item(),
                        'fc/correct_prob': correct_prob/(correct_num+0.001), 'fc/false_prob': false_prob/(false_num+0.001),
                        'fc/correct_num': correct_num, 'fc/false_num': false_num, 'fc/diff_prob': correct_prob/(correct_num+0.001)-false_prob/(false_num+0.001),
                    }
                    append_to_dict(metrics, data)
        self.actor_optimizer.zero_grad()
        if self.use_score:
            self.score_optimizer.zero_grad()
        return metrics
