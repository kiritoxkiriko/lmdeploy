# Copyright (c) OpenMMLab. All rights reserved.

from typing import Any, Optional, Tuple, Union

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.utils.checkpoint
from transformers.cache_utils import Cache
from transformers.modeling_outputs import MoeModelOutputWithPast

from lmdeploy.pytorch.kernels.fused_moe import fused_moe

from ..kernels import fill_kv_cache, fused_rotary_emb, paged_attention_fwd
from ..weight_loader.dist_utils import (colwise_split_parallelize_linear,
                                        rowwise_parallelize_linear)


class PatchedDbrxAttention(nn.Module):

    def _load_weights(self, loader, rank: int, world_size: int,
                      device: torch.device):
        """load weights."""
        sections = [
            self.num_heads * self.head_dim,
            self.num_key_value_heads * self.head_dim,
            self.num_key_value_heads * self.head_dim,
        ]
        colwise_split_parallelize_linear(self.Wqkv,
                                         sections,
                                         loader,
                                         rank=rank,
                                         world_size=world_size,
                                         prefix='Wqkv')
        rowwise_parallelize_linear(self.out_proj,
                                   loader,
                                   rank=rank,
                                   world_size=world_size,
                                   prefix='out_proj')

    @classmethod
    def _distribute_output_fn(cls, outputs, **kwargs):
        """Distribution output hook."""
        dist.all_reduce(outputs[0])
        return outputs

    def _contiguous_batching_forward_impl(
        self,
        hidden_states: torch.Tensor,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        world_size: int = 1,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor],
               Optional[Tuple[torch.Tensor]]]:
        """Implement of attention forward."""
        context = self.context.context
        q_start_loc = context.q_start_loc
        q_seq_length = context.q_seq_length
        kv_seq_length = context.kv_seq_length
        block_offsets = context.block_offsets
        max_q_seq_length = context.max_q_seq_length

        num_heads = self.num_heads // world_size
        num_kv_heads = self.num_key_value_heads // world_size
        head_dim = self.head_dim

        def __qkv_proj(hidden_states):
            """qkv_proj."""
            qkv_states = self.Wqkv(hidden_states)
            if self.clip_qkv is not None:
                qkv_states = qkv_states.clamp(min=-self.clip_qkv,
                                              max=self.clip_qkv)

            query_states, key_states, value_states = qkv_states.split(
                [
                    num_heads * head_dim,
                    num_kv_heads * head_dim,
                    num_kv_heads * head_dim,
                ],
                dim=-1,
            )

            query_states = query_states.view(-1, num_heads, head_dim)
            key_states = key_states.view(-1, num_kv_heads, head_dim)
            value_states = value_states.view(-1, num_kv_heads, head_dim)
            return query_states, key_states, value_states

        def __rotary_emb_fn(query_states, key_states, value_states):
            scaling_factor = 1.0
            rotary_emb = self.rotary_emb
            if rotary_emb.inv_freq is None:
                rotary_emb.inv_freq = 1.0 / (rotary_emb.base**(torch.arange(
                    0,
                    rotary_emb.dim,
                    2,
                    dtype=torch.int64,
                    device=query_states.device).float() / rotary_emb.dim))
            inv_freq = rotary_emb.inv_freq
            query_states, key_states = fused_rotary_emb(
                query_states[None],
                key_states[None],
                context.position_ids_1d[None],
                inv_freq=inv_freq,
                scaling_factor=scaling_factor,
                out_q=query_states[None],
                out_k=key_states[None])
            return query_states[0], key_states[0], value_states

        query_states, key_states, value_states = __qkv_proj(hidden_states)

        query_states, key_states, value_states = __rotary_emb_fn(
            query_states, key_states, value_states)

        fill_kv_cache(
            key_states,
            value_states,
            past_key_value[0],
            past_key_value[1],
            q_start_loc,
            q_seq_length,
            kv_seq_length=kv_seq_length,
            max_q_seq_length=max_q_seq_length,
            block_offsets=block_offsets,
        )

        attn_output = query_states
        paged_attention_fwd(
            query_states,
            past_key_value[0],
            past_key_value[1],
            attn_output,
            block_offsets,
            q_start_loc=q_start_loc,
            q_seqlens=q_seq_length,
            kv_seqlens=kv_seq_length,
            max_seqlen=max_q_seq_length,
        )
        attn_output = attn_output.reshape(*hidden_states.shape[:-1], -1)

        attn_output = self.out_proj(attn_output)

        return attn_output, None, past_key_value

    def forward(
        self,
        hidden_states: torch.Tensor,
        position_ids: torch.LongTensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_value: Optional[Cache] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        cache_position: Optional[torch.LongTensor] = None,
        **kwargs: Any,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Cache]]:
        """forward."""
        world_size = 1
        if dist.is_initialized():
            world_size = dist.get_world_size()
        return self._contiguous_batching_forward_impl(
            hidden_states,
            past_key_value,
            world_size=world_size,
        )


class PatchedDbrxExpertGLU(nn.Module):

    def _load_weights(self, loader, rank: int, world_size: int,
                      device: torch.device):
        """load weights."""

        def __partition(name, param):
            param = loader.pop(name)
            param = param.unflatten(0, (self.moe_num_experts, -1))
            param = param.chunk(world_size, 1)[rank]
            param = param.to(device)
            dtype = param.dtype
            if param.dtype != dtype:
                param = param.to(dtype)
            param = torch.nn.Parameter(param.flatten(0, 1))
            self.register_parameter(name, param)

        __partition('w1', self.w1)
        __partition('v1', self.v1)
        __partition('w2', self.w2)

    def _update_model_fn(self):
        """update model."""
        ffn_hidden_size = self.w1.size(0) // self.moe_num_experts
        gate_up_weights = self.w1.new_empty(self.moe_num_experts,
                                            ffn_hidden_size * 2,
                                            self.w1.size(1))
        gate_up_weights[:, :ffn_hidden_size].copy_(
            self.w1.unflatten(0, (self.moe_num_experts, -1)))
        gate_up_weights[:, ffn_hidden_size:].copy_(
            self.v1.unflatten(0, (self.moe_num_experts, -1)))
        delattr(self, 'w1')
        delattr(self, 'v1')
        down_weights = self.w2.data.unflatten(
            0, (self.moe_num_experts, -1)).transpose(1, 2).contiguous()
        delattr(self, 'w2')
        torch.cuda.empty_cache()

        self.register_buffer('gate_up_weights', gate_up_weights)
        self.register_buffer('down_weights', down_weights)


class PatchedDbrxExperts(nn.Module):

    @classmethod
    def _distribute_output_fn(cls, outputs, **kwargs):
        """Distribution output hook."""
        dist.all_reduce(outputs)
        return outputs

    def forward(self, x: torch.Tensor, weights: torch.Tensor,
                top_weights: torch.Tensor,
                top_experts: torch.LongTensor) -> torch.Tensor:
        """moe forward."""
        q_len = x.size(1)
        x = x.flatten(0, 1)
        out_states = fused_moe(x,
                               self.mlp.gate_up_weights,
                               self.mlp.down_weights,
                               top_weights,
                               top_experts,
                               topk=top_weights.size(1),
                               renormalize=False)

        out_states = out_states.unflatten(0, (-1, q_len))
        return out_states


class PatchedDbrxModel(nn.Module):

    def _continuous_batching_forward(
        self,
        input_ids: Optional[torch.LongTensor],
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
    ) -> Union[Tuple, MoeModelOutputWithPast]:
        """forward impl."""
        output_attentions = False
        use_cache = True
        output_router_logits = False

        inputs_embeds = self.wte(input_ids)

        # Attention mask is not necessary in continuous batching
        attention_mask = None
        cache_position = None

        hidden_states = inputs_embeds

        for idx, block in enumerate(self.blocks):
            past_key_value = past_key_values[idx]
            block_outputs = block(
                hidden_states,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_value=past_key_value,
                output_attentions=output_attentions,
                output_router_logits=output_router_logits,
                use_cache=use_cache,
                cache_position=cache_position,
            )
            hidden_states = block_outputs[0]

        hidden_states = self.norm_f(hidden_states)

        return MoeModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=past_key_values,
            hidden_states=None,
            attentions=None,
        )

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_router_logits: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
    ) -> Union[Tuple, MoeModelOutputWithPast]:
        """Rewrite of LlamaModel.forward."""
        return self._continuous_batching_forward(
            input_ids,
            position_ids,
            past_key_values,
        )
