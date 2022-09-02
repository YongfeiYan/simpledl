import math
import torch
from torch.nn.init import xavier_normal_, xavier_uniform_, constant_
from torch.nn import Module, Parameter, Linear, functional as F


class MultiheadAttention(Module):
    r"""Allows the model to jointly attend to information
    from different representation subspaces.
    See reference: Attention Is All You Need

    .. math::
        \text{MultiHead}(Q, K, V) = \text{Concat}(head_1,\dots,head_h)W^O
        \text{where} head_i = \text{Attention}(QW_i^Q, KW_i^K, VW_i^V)

    Args:
        embed_dim: total dimension of the model.
        num_heads: parallel attention heads.
        dropout: a Dropout layer on attn_output_weights. Default: 0.0.
        bias: add bias as module parameter. Default: True.
        add_bias_kv: add bias to the key and value sequences at dim=0.
        add_zero_attn: add a new batch of zeros to the key and
                       value sequences at dim=1.
        kdim: total number of features in key. Default: None.
        vdim: total number of features in key. Default: None.

        Note: if kdim and vdim are None, they will be set to embed_dim such that
        query, key, and value have the same number of features.

    Examples::

        >>> multihead_attn = nn.MultiheadAttention(embed_dim, num_heads)
        >>> attn_output, attn_output_weights = multihead_attn(query, key, value)
    """
    __annotations__ = {
        'bias_k': torch._jit_internal.Optional[torch.Tensor],
        'bias_v': torch._jit_internal.Optional[torch.Tensor],
    }
    __constants__ = ['q_proj_weight', 'k_proj_weight', 'v_proj_weight', 'in_proj_weight']

    def __init__(self, embed_dim, num_heads, dropout=0., bias=True, add_bias_kv=False, add_zero_attn=False, kdim=None, vdim=None):
        super(MultiheadAttention, self).__init__()
        self.embed_dim = embed_dim
        self.kdim = kdim if kdim is not None else embed_dim
        self.vdim = vdim if vdim is not None else embed_dim
        self._qkv_same_embed_dim = self.kdim == embed_dim and self.vdim == embed_dim

        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        self.scaling = self.head_dim ** -0.5
        assert self.head_dim * num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"
        assert self._qkv_same_embed_dim, '_qkv_same_embed_dim is False 的情况还没考虑.'

        if self._qkv_same_embed_dim is False:
            self.q_proj_weight = Parameter(torch.Tensor(embed_dim, embed_dim))
            self.k_proj_weight = Parameter(torch.Tensor(embed_dim, self.kdim))
            self.v_proj_weight = Parameter(torch.Tensor(embed_dim, self.vdim))
            self.register_parameter('in_proj_weight', None)
        else:
            self.in_proj_weight = Parameter(torch.empty(3 * embed_dim, embed_dim))
            self.register_parameter('q_proj_weight', None)
            self.register_parameter('k_proj_weight', None)
            self.register_parameter('v_proj_weight', None)

        if bias:
            self.in_proj_bias = Parameter(torch.empty(3 * embed_dim))
        else:
            self.register_parameter('in_proj_bias', None)
        self.out_proj = Linear(embed_dim, embed_dim, bias=bias)

        if add_bias_kv:
            self.bias_k = Parameter(torch.empty(1, 1, embed_dim))
            self.bias_v = Parameter(torch.empty(1, 1, embed_dim))
        else:
            self.bias_k = self.bias_v = None

        self.add_zero_attn = add_zero_attn
        assert add_zero_attn == False, '不支持add_zero_attn'

        self._reset_parameters()

    def _reset_parameters(self):
        if self._qkv_same_embed_dim:
            xavier_uniform_(self.in_proj_weight)  # 下述是fairseq的初始化方式, torch的实现gain不一样.
            # xavier_uniform_(self.k_proj_weight, gain=1 / math.sqrt(2))
            # xavier_uniform_(self.v_proj_weight, gain=1 / math.sqrt(2))
            # xavier_uniform_(self.q_proj_weight, gain=1 / math.sqrt(2))
        else:
            xavier_uniform_(self.q_proj_weight)
            xavier_uniform_(self.k_proj_weight)
            xavier_uniform_(self.v_proj_weight)
        # 下一行是添加的, 原先没有.
        xavier_uniform_(self.out_proj.weight)
        
        if self.in_proj_bias is not None:
            constant_(self.in_proj_bias, 0.)
            constant_(self.out_proj.bias, 0.)
        if self.bias_k is not None:
            xavier_normal_(self.bias_k)
        if self.bias_v is not None:
            xavier_normal_(self.bias_v)

    def __setstate__(self, state):
        super(MultiheadAttention, self).__setstate__(state)

        # Support loading old MultiheadAttention checkpoints generated by v1.1.0
        if 'self._qkv_same_embed_dim' not in self.__dict__:
            self._qkv_same_embed_dim = True

    def forward(self, query, key, value, key_padding_mask=None,
                attn_mask=None, static_kv=False, prev_state=None):
        # type: (Tensor, Tensor, Tensor, Optional[Tensor], bool, Optional[Tensor]) -> Tuple[Tensor, Optional[Tensor]]
        r"""
    Args:
        query, key, value: map a query and a set of key-value pairs to an output.
            See "Attention Is All You Need" for more details.
        key_padding_mask: if provided, specified padding elements in the key will
            be ignored by the attention. This is an binary mask. When the value is True,
            the corresponding value on the attention layer will be filled with -inf.
        need_weights: output attn_output_weights.
        attn_mask: mask that prevents attention to certain positions. This is an additive mask
            (i.e. the values will be added to the attention layer).

    Shape:
        - Inputs:
        - query: :math:`(L, N, E)` where L is the target sequence length, N is the batch size, E is
          the embedding dimension.
        - key: :math:`(S, N, E)`, where S is the source sequence length, N is the batch size, E is
          the embedding dimension.
        - value: :math:`(S, N, E)` where S is the source sequence length, N is the batch size, E is
          the embedding dimension.
        - key_padding_mask: :math:`(N, S)`, ByteTensor, where N is the batch size, S is the source sequence length.
        - attn_mask: :math:`(L, S)` where L is the target sequence length, S is the source sequence length.
        - static_kv: 如果是static kv, 循环调用的时候, key value都是相同的, 有些计算可以避免. 只计算query相关的.

        - Outputs:
        - attn_output: :math:`(L, N, E)` where L is the target sequence length, N is the batch size,
          E is the embedding dimension.
        - attn_output_weights: :math:`(N, L, S)` where N is the batch size,
          L is the target sequence length, S is the source sequence length.
        """
        if prev_state is not None:
            return self._recurrent_forward(query, key, value, key_padding_mask, attn_mask, static_kv, prev_state)

        if not self._qkv_same_embed_dim:
            return F.multi_head_attention_forward(
                query, key, value, self.embed_dim, self.num_heads,
                self.in_proj_weight, self.in_proj_bias,
                self.bias_k, self.bias_v, self.add_zero_attn,
                self.dropout, self.out_proj.weight, self.out_proj.bias,
                training=self.training,
                key_padding_mask=key_padding_mask, need_weights=True,
                attn_mask=attn_mask, use_separate_proj_weight=True,
                q_proj_weight=self.q_proj_weight, k_proj_weight=self.k_proj_weight,
                v_proj_weight=self.v_proj_weight)
        else:
            return F.multi_head_attention_forward(
                query, key, value, self.embed_dim, self.num_heads,
                self.in_proj_weight, self.in_proj_bias,
                self.bias_k, self.bias_v, self.add_zero_attn,
                self.dropout, self.out_proj.weight, self.out_proj.bias,
                training=self.training,
                key_padding_mask=key_padding_mask, need_weights=True,
                attn_mask=attn_mask)

    def _recurrent_forwardxx(self, query, key, value, key_padding_mask=None, attn_mask=None, static_kv=False, prev_state=None):
        # key, value: S x bsz x D
        # key_padding_mask: bsz x S
        if static_kv:
            attn, attn_weights = self.forward(query, key, value, key_padding_mask, attn_mask, static_kv, prev_state=None)
            return attn, attn_weights, prev_state
        new_state = {}
        if 'prev_key' in prev_state:
            key = torch.cat([prev_state['prev_key'].transpose(0, 1), key], dim=0)
            value = torch.cat([prev_state['prev_value'].transpose(0, 1), value], dim=0)
            if key_padding_mask is not None:
                key_padding_mask = torch.cat([prev_state['prev_key_padding_mask'], key_padding_mask], dim=1)
        new_state['prev_key'] = key.transpose(0, 1)
        new_state['prev_value'] = value.transpose(0, 1)
        if key_padding_mask is not None:
            new_state['prev_key_padding_mask'] = key_padding_mask
        attn, attn_weights = self.forward(query, key, value, key_padding_mask, attn_mask, static_kv)
        return attn, attn_weights, new_state

    def _recurrent_forward(self, query, key, value, key_padding_mask=None,
        attn_mask=None, static_kv=False, prev_state=None):
        """
        attn_mask: 1 x t, t是当前位置.
        1. static_kv的情况(类似decoder到encoder的attn): 如果key value之前没有经过矩阵计算, 就计算然后保存
            key_padding_mask: B x S
        2. 否则的话(类似decoder的self attn), key value在attn之前要先拼接存储的 key value
            key_padding_mask: B x 1
        """
        tgt_len, bsz, _ = query.size()
        src_len = key.size(0)
        # TODO delete
        # dels = {}
        # Get saved state
        static_key = prev_state.get('static_key', None)  # bsz x num_heads x S x head_dim
        static_value = prev_state.get('static_value', None)  # bsz x num_heads x S x head_dim
        prev_key = prev_state.get('prev_key', None)  # bsz x num_heads x S_i x head_dim
        prev_value = prev_state.get('prev_value', None)  # bsz x num_heads x S_i x head_dim
        # prev_attn_mask = prev_state.get('prev_attn_mask', None)  # bsz x Ti x Si, 每个batch内的mask都相同
        prev_key_padding_mask = prev_state.get('prev_key_padding_mask', None)  # bsz x Si, 
        new_state = {}

        if self._qkv_same_embed_dim:
            q_proj_weight, k_proj_weight, v_proj_weight = torch.chunk(self.in_proj_weight, 3, dim=0)
            q_proj_bias, k_proj_bias, v_proj_bias = torch.chunk(self.in_proj_bias, 3, dim=0)

            query = F.linear(query, q_proj_weight, q_proj_bias)
            if static_kv:
                if static_key is None:
                    key = F.linear(key, k_proj_weight, k_proj_bias)
                    value = F.linear(value, v_proj_weight, v_proj_bias)
            else:
                key = F.linear(key, k_proj_weight, k_proj_bias)
                value = F.linear(value, v_proj_weight, v_proj_bias)
        else:
            raise NotImplementedError('')

        query = query * self.scaling

        # bsz*num_heads x seq_len x head_dim
        query = query.contiguous().view(-1, bsz * self.num_heads, self.head_dim).transpose(0, 1)
        if static_kv and static_key is not None:
            key = static_key.contiguous().view(bsz * self.num_heads, -1, self.head_dim)
            value = static_value.contiguous().view(bsz * self.num_heads, -1, self.head_dim)
        else:
            key = key.contiguous().view(-1, bsz * self.num_heads, self.head_dim).transpose(0, 1)
            value = value.contiguous().view(-1, bsz * self.num_heads, self.head_dim).transpose(0, 1)
            if static_kv:
                new_state['static_key'] = key.contiguous().view(bsz, self.num_heads, -1, self.head_dim)
                new_state['static_value'] = value.contiguous().view(bsz, self.num_heads, -1, self.head_dim)
            else:
                if prev_key is not None:
                    key = torch.cat([prev_key.contiguous().view(bsz * self.num_heads, -1, self.head_dim), key], dim=1)
                    value = torch.cat([prev_value.contiguous().view(bsz * self.num_heads, -1, self.head_dim), value], dim=1)
                    src_len = key.size(1)
                new_state['prev_key'] = key.contiguous().view(bsz, self.num_heads, -1, self.head_dim)
                new_state['prev_value'] = value.contiguous().view(bsz, self.num_heads, -1, self.head_dim)
        # dels.update({
        #     'linq': query, 
        #     'link': key.transpose(0, 1).contiguous().view(-1, bsz, self.embed_dim), 
        #     'linv': value.transpose(0, 1).contiguous().view(-1, bsz, self.embed_dim)
        # })

        attn_weights = torch.bmm(query, key.transpose(1, 2))  # bsz*num_heads x T x S
        # dels['qkt'] = attn_weights

        # print('shape attn, query, key', attn_weights.shape, query.shape, key.shape)
        if attn_mask is not None:
            attn_mask = attn_mask.unsqueeze(0)  # 1 x 1 x Si+1
            # if not static_kv:
            #     if prev_attn_mask is not None:
            #         # prev_attn_mask: bsz x Ti x Si -> bsz x Ti x Si+1
            #         zero_column = torch.zeros(bsz, prev_attn_mask.size(1), 1).type_as(prev_attn_mask)
            #         prev_attn_mask = torch.cat([prev_attn_mask, zero_column], dim=-1)
            #         attn_mask = torch.cat([prev_attn_mask, attn_mask.repeat(bsz, 1, 1)], dim=1)  # bsz x Ti+1 x Si+1
            #     else:
            #         attn_mask = attn_mask.repeat(bsz, 1, 1)
            #     new_state['prev_attn_mask'] = attn_mask
            # print(attn_weights.shape, attn_mask.shape)
            # attn_weights = attn_weights + attn_mask.repeat(self.num_heads, 1, 1)
            attn_weights = attn_weights + attn_mask
        # dels['qkt attn mask'] = attn_weights
        if key_padding_mask is not None:
            # static_kv的时候, 每次调用都会传递一次key_padding_mask, 不用存储. 否则的话, 加上当前时刻的key_padding_mask
            if not static_kv:
                if prev_key_padding_mask is not None:
                    key_padding_mask = torch.cat([prev_key_padding_mask, key_padding_mask], dim=1)
                new_state['prev_key_padding_mask'] = key_padding_mask
            # print('attn weights shape', attn_weights.shape, 'tgt len', tgt_len, 'src len', src_len)
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            # print('attn weights shape', attn_weights.shape, 'tgt len', tgt_len, 'src len', src_len)
            attn_weights = attn_weights.masked_fill(key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool), 
                float('-inf')
            )
            # print('attn weights shape', attn_weights.shape, 'tgt len', tgt_len, 'src len', src_len, 'key padding mask', key_padding_mask.shape)
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)
        # dels['qkt pad mask'] = attn_weights
        attn_probs = F.softmax(attn_weights, dim=-1, dtype=attn_weights.dtype)
        # dels['softmax'] = attn_probs
        # print(attn_probs)
        attn_probs = F.dropout(attn_probs, p=self.dropout, training=self.training)  # bsz*num_heads x tgt_len x src_len
        # TODO: 保证pad位置不被自己修改的pad, drop掉, delete masked fill
        # print(attn_probs)
        # attn_probs.masked_fill_(key_padding_mask.unsqueeze(1).repeat(self.num_heads, tgt_len, 1), 0)
        # print(attn_probs)
        # print('dropout', self.dropout)
        if tgt_len > 1:
            raise RuntimeError()
        # dels['dropout'] = attn_probs
        attn = torch.bmm(attn_probs, value).transpose(0, 1).contiguous().view(tgt_len, bsz, self.embed_dim)  # tgt_len x bsz*num_heads x embed_dim
        # dels['bmm'] = attn
        attn = self.out_proj(attn)
        # dels['linear'] = attn
        # new_state.update(dels)
        
        return attn, attn_weights, new_state


