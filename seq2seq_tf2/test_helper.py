import tensorflow as tf
import numpy as np
from seq2seq_tf2.batcher import output_to_words
from tqdm import tqdm
import math


def greedy_decode(model, dataset, vocab, params):
    # 存储结果
    batch_size = params["batch_size"]
    results = []

    sample_size = 20000
    # batch 操作轮数 math.ceil向上取整 小数 +1
    # 因为最后一个batch可能不足一个batch size 大小 ,但是依然需要计算
    steps_epoch = sample_size // batch_size + 1
    #print(dataset)
    #enc_data, _ = next(iter(dataset))
    #把 dataset变成迭代对象
    dataset = iter(dataset)
    for i in tqdm(range(steps_epoch)):
        enc_data, _ = next(dataset)
        results += batch_greedy_decode(model, enc_data, vocab, params)
    return results


def batch_greedy_decode(model, enc_data, vocab, params):
    # 判断输入长度
    batch_data = enc_data["enc_input"]
    batch_size = enc_data["enc_input"].shape[0]
    # 开辟结果存储list
    predicts = [''] * batch_size
    # inputs = batch_data # shape=(3, 115)
    # 输入batch转成tensor
    inputs = tf.convert_to_tensor(batch_data)
    #print(inputs)
    # hidden = [tf.zeros((batch_size, params['enc_units']))]
    # enc_output, enc_hidden = model.encoder(inputs, hidden)
    enc_output, enc_hidden = model.call_encoder(inputs)

    dec_hidden = enc_hidden
    # dec_input = tf.expand_dims([vocab.word_to_id(vocab.START_DECODING)] * batch_size, 1)
    dec_input = tf.constant([2] * batch_size) # 2 是 [START] 对应的token
    dec_input = tf.expand_dims(dec_input, axis=1)

    context_vector, _ = model.attention(dec_hidden, enc_output)
    for t in range(params['max_dec_len']):
        # 单步预测
        _, pred, dec_hidden = model.decoder(dec_input, dec_hidden, enc_output, context_vector)
        context_vector, _ = model.attention(dec_hidden, enc_output)
        # 取出网络输出的预测向量中最大值的下标，然后Tensor2Numpy
        predicted_ids = tf.argmax(pred, 1).numpy()
        for index, predicted_id in enumerate(predicted_ids):
            predicts[index] += vocab.id_to_word(predicted_id) + ' '

        # using teacher forcing
        #print('dec_input1 is',dec_input.shape)
        dec_input = tf.expand_dims(predicted_ids, 1)
        #print('dec_input2 is',dec_input.shape)

    results = []
    for predict in predicts:
        # 去掉句子前后空格
        predict = predict.strip()
        # 句子小于max len就结束了 截断vocab.word_to_id('[STOP]')
        if '[STOP]' in predict:
            # 截断stop
            predict = predict[:predict.index('[STOP]')]
        # 保存结果
        results.append(predict)
    # print(len(results))
    return results


def beam_decode(model, dataset, vocab, params):
    # 存储结果
    batch_size = params["batch_size"]
    results = []

    sample_size = 20000
    # batch 操作轮数 math.ceil向上取整 小数 +1
    # 因为最后一个batch可能不足一个batch size 大小 ,但是依然需要计算
    steps_epoch = sample_size // batch_size
    print(steps_epoch)
    #steps_epoch = math.ceil(sample_size / batch_size)
    #print(dataset)
    dataset = iter(dataset)
    for i in tqdm(range(steps_epoch)):
        enc_data, _ = next(dataset)
        #print(enc_data)
        #enc_data = tf.convert_to_tensor(enc_data["enc_input"])
        best_hyp = batch_beam_decode(model, enc_data, vocab, params)
        results.append(best_hyp.abstract)
        #print(results)
    return results

class Hypothesis:
    """ Class designed to hold hypothesises throughout the beamSearch decoding """

    def __init__(self, tokens, log_probs, state, attn_dists):
        # list of all the tokens from time 0 to the current time step t
        self.tokens = tokens
        # list of the log probabilities of the tokens of the tokens
        self.log_probs = log_probs
        # decoder state after the last token decoding
        self.state = state
        # attention dists of all the tokens
        self.attn_dists = attn_dists


    def extend(self, token, log_prob, state, attn_dist):
        """Method to extend the current hypothesis by adding the next decoded token and all
        the informations associated with it"""
        return Hypothesis(tokens=self.tokens + [token],  # we add the decoded token
                          log_probs=self.log_probs + [log_prob],  # we add the log prob of the decoded token
                          state=state,  # we update the state
                          attn_dists=self.attn_dists + [attn_dist]
                          # we  add the attention dist of the decoded token
                          )

    @property
    def latest_token(self):
        return self.tokens[-1]

    @property
    def tot_log_prob(self):
        return sum(self.log_probs)

    @property
    def avg_log_prob(self):
        return self.tot_log_prob / len(self.tokens)


def batch_beam_decode(model, batch, vocab, params):

    def decode_onestep(model, enc_output, dec_input, dec_state):
        """
            Method to decode the output step by step (used for beamSearch decoding)
        """
        context_vector, attentions = model.attention(dec_state, enc_output)

        _, final_dists, dec_hidden = model.decoder(dec_input,
                                        None,
                                        enc_output,
                                        context_vector)

        top_k_probs, top_k_ids = tf.nn.top_k(tf.squeeze(final_dists), k=params["beam_size"] * 2)
        #print('top_k_ids ', top_k_ids)
        top_k_log_probs = tf.math.log(top_k_probs)

        results = {"dec_state": dec_hidden,
                   "attention_vec": attentions,
                   "top_k_ids": top_k_ids,
                   "top_k_log_probs": top_k_log_probs
                   }
        return results

    # end of the nested class

    # We run the encoder once and then we use the results to decode each time step token

    enc_outputs, state = model.call_encoder(batch["enc_input"])
    # batch_size个对象的列表
    hyps = [Hypothesis(tokens=[vocab.word_to_id('[START]')],
                       log_probs=[0.0],
                       state=state[0],
                       #p_gens=[],
                       attn_dists=[]) for _ in range(params['batch_size'])]

    results = []  # list to hold the top beam_size hypothesises
    steps = 0  # initial step

    while steps < params['max_dec_steps'] and len(results) < params['beam_size']:
        # print('step is ', steps)
        latest_tokens = [h.latest_token for h in hyps]  # latest token for each hypothesis , shape : [beam_size]
        # print('latest_tokens is ', latest_tokens)
        # we replace all the oov is by the unknown token
        # print(latest_tokens)
        latest_tokens = [t if t in range(params['vocab_size']) else vocab.word_to_id('[UNK]') for t in latest_tokens]
        # latest_tokens = [t if t in vocab.id2word else vocab.word2id('[UNK]') for t in latest_tokens]
        # print('latest_tokens is ', latest_tokens)
        # we collect the last states for each hypothesis
        # print(latest_tokens)
        states = [h.state for h in hyps]
        # print('states i s', states)

        # we decode the top likely 2 x beam_size tokens tokens at time step t for each hypothesis
        # model, batch, vocab, params
        dec_input = tf.expand_dims(latest_tokens, axis=1)  # shape=(3, 1)
        # print('dec_input is ', dec_input.shape)
        # print('step is ', steps)
        # print('dec_input is ', dec_input)
        # print('states is ', states)
        # tf.stack([A,B,C], axis=0),输出张量形状为（N，A，B，C），如果axis=1，输出张量形状为（A，N，B，C）。
        dec_states = tf.stack(states, axis=0)
        #print(dec_states)

        returns = decode_onestep(model,
                                 enc_outputs,  # shape=(3, 115, 256)
                                 dec_input,  # shape=(3, 1)
                                 dec_states,  # shape=(3, 256)
                                 )
        topk_ids = returns['top_k_ids']
        #topk_ids = tf.expand_dims(topk_ids, 0)
        #topk_ids = tf.expand_dims(topk_ids, 1)
        #print(topk_ids)
        topk_log_probs = returns['top_k_log_probs']
        #topk_log_probs = tf.expand_dims(topk_log_probs, 0)
        new_states = returns['dec_state']
        attn_dists = returns['attention_vec']
        all_hyps = list()
        # print('topk_ids is ', topk_ids)
        # print('topk_log_probs is ', topk_log_probs)

        #all_hyps = []
        num_orig_hyps = 1 if steps == 0 else len(hyps)
        num = 1
        # print('num_orig_hyps is ', num_orig_hyps)
        for i in range(num_orig_hyps):
            h, new_state, attn_dist = hyps[i], new_states[i], attn_dists[i]
            # print('h is ', h)

            num += 1
            # print('num is ', num)
            # all_hyps 中收集所有组合，一共beam_size*beam_size*2个
            for j in range(params['beam_size'] * 2):
                # we extend each hypothesis with each of the top k tokens

                new_hyp = h.extend(token=topk_ids[i, j].numpy(),
                                   log_prob=topk_log_probs[i, j],
                                   state=new_state,
                                   attn_dist=attn_dist
                                   )
                all_hyps.append(new_hyp)
        # in the following lines, we sort all the hypothesises, and select only the beam_size most likely hypothesises
        hyps = []
        sorted_hyps = sorted(all_hyps, key=lambda h: h.avg_log_prob, reverse=True)
        #print(len(sorted_hyps))
        # 选出前beam_size 个的
        for h in sorted_hyps:
            if h.latest_token == vocab.word_to_id('[STOP]'):
                if steps >= params['min_dec_steps']:
                    results.append(h)
            else:
                # print(h.latest_token)
                hyps.append(h)
            if len(hyps) == params['beam_size'] or len(results) == params['beam_size']:
                break
        # print('hyps is ', hyps.)
        # print('steps is ', steps)
        steps += 1

    if len(results) == 0:
        results = hyps
    #print(len(results))
    # At the end of the loop we return the most likely hypothesis, which holds the most likely ouput sequence,
    # given the input fed to the model

    # 从大到小排序
    hyps_sorted = sorted(results, key=lambda h: h.avg_log_prob, reverse=True)

    # 选概率最大的返回
    best_hyp = hyps_sorted[0]
    best_hyp.abstract = " ".join(output_to_words(best_hyp.tokens, vocab, batch["article_oovs"][0])[1:-1])
    #best_hyp.text = batch[0]["article"].numpy()[0].decode()
    #print('best_hyp is ', best_hyp.abstract)
    return best_hyp



