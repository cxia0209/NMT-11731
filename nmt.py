# coding=utf-8

"""
A very basic implementation of neural machine translation

Usage:
    nmt.py train --train-src=<file> --train-tgt=<file> --dev-src=<file> --dev-tgt=<file> --vocab=<file> [options]
    nmt.py decode [options] MODEL_PATH TEST_SOURCE_FILE OUTPUT_FILE
    nmt.py decode [options] MODEL_PATH TEST_SOURCE_FILE TEST_TARGET_FILE OUTPUT_FILE

Options:
    -h --help                               show this screen.
    --cuda                                  use GPU
    --train-src=<file>                      train source file
    --train-tgt=<file>                      train target file
    --dev-src=<file>                        dev source file
    --dev-tgt=<file>                        dev target file
    --vocab=<file>                          vocab file
    --seed=<int>                            seed [default: 0]
    --batch-size=<int>                      batch size [default: 32]
    --embed-size=<int>                      embedding size [default: 256]
    --hidden-size=<int>                     hidden size [default: 256]
    --clip-grad=<float>                     gradient clipping [default: 5.0]
    --log-every=<int>                       log every [default: 10]
    --max-epoch=<int>                       max epoch [default: 30]
    --patience=<int>                        wait for how many iterations to decay learning rate [default: 5]
    --max-num-trial=<int>                   terminate training after how many trials [default: 5]
    --lr-decay=<float>                      learning rate decay [default: 0.5]
    --beam-size=<int>                       beam size [default: 5]
    --lr=<float>                            learning rate [default: 0.001]
    --uniform-init=<float>                  uniformly initialize all parameters [default: 0.1]
    --save-to=<file>                        model save path
    --valid-niter=<int>                     perform validation after how many iterations [default: 2000]
    --dropout=<float>                       dropout [default: 0.2]
    --max-decoding-time-step=<int>          maximum number of decoding time steps [default: 70]
"""

import math
import pickle
import sys
import time
from collections import namedtuple

import numpy as np
from typing import List, Tuple, Dict, Set, Union
from docopt import docopt
from tqdm import tqdm
from nltk.translate.bleu_score import corpus_bleu, sentence_bleu, SmoothingFunction

from utils import read_corpus, batch_iter
from vocab import Vocab, VocabEntry

import torch
import torch.nn as nn
from torch.autograd import Variable

from encoder import Encoder
from decoder import Decoder
from torch import optim

from loss import NLLLoss
from optim import Optimizer


Hypothesis = namedtuple('Hypothesis', ['value', 'score'])


class NMT(object):

    def __init__(self, embed_size, hidden_size, vocab, dropout_rate=0.2,keep_train=False):
        super(NMT, self).__init__()

        self.nvocab_src = len(vocab.src)
        self.nvocab_tgt = len(vocab.tgt)
        self.vocab = vocab
        self.encoder = Encoder(self.nvocab_src, hidden_size, embed_size, input_dropout=dropout_rate, n_layers=2)
        self.decoder = Decoder(self.nvocab_tgt, 2*hidden_size, embed_size,output_dropout=dropout_rate, n_layers=2,tf_rate=1.0)
        if keep_train:
            self.load('model')
        LAS_params = list(self.encoder.parameters()) + list(self.decoder.parameters())
        self.optimizer = optim.Adam(LAS_params, lr=0.0001)
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=1, gamma=0.5)
        weight = torch.ones(self.nvocab_tgt)
        self.loss = NLLLoss(weight=weight, mask=0, size_average=False)
         # TODO: Perplexity or NLLLoss
        # TODO: pass in mask to loss funciton
        #self.loss = Perplexity(weight, 0)

        if torch.cuda.is_available():
            # Move the network and the optimizer to the GPU
            self.encoder = self.encoder.cuda()
            self.decoder = self.decoder.cuda()
            self.loss.cuda()


    def __call__(self, src_sents, tgt_sents):
        """
        take a mini-batch of source and target sentences, compute the log-likelihood of 
        target sentences.

        Args:
            src_sents: list of source sentence tokens
            tgt_sents: list of target sentence tokens, wrapped by `<s>` and `</s>`

        Returns:
            scores: a variable/tensor of shape (batch_size, ) representing the 
                log-likelihood of generating the gold-standard target sentence for 
                each example in the input batch
        """
        src_sents = self.vocab.src.words2indices(src_sents)
        tgt_sents = self.vocab.tgt.words2indices(tgt_sents)
        src_sents, src_len, y_input, y_tgt, tgt_len  = sent_padding(src_sents, tgt_sents)
        src_encodings, decoder_init_state = self.encode(src_sents,src_len)
        scores, symbols = self.decode(src_encodings, decoder_init_state, [y_input, y_tgt], stage="train")

        return scores

    def encode(self, src_sents, input_lengths):
        """
        Use a GRU/LSTM to encode source sentences into hidden states

        Args:
            src_sents: list of source sentence tokens

        Returns:
            src_encodings: hidden states of tokens in source sentences, this could be a variable 
                with shape (batch_size, source_sentence_length, encoding_dim), or in orther formats
            decoder_init_state: decoder GRU/LSTM's initial state, computed from source encodings
        """
        encoder_outputs, encoder_hidden = self.encoder(src_sents,input_lengths)

        return encoder_outputs, encoder_hidden


    def decode(self, src_encodings, decoder_init_state, tgt_sents, stage="train"):
        """
        Given source encodings, compute the log-likelihood of predicting the gold-standard target
        sentence tokens

        Args:
            src_encodings: hidden states of tokens in source sentences
            decoder_init_state: decoder GRU/LSTM's initial state
            tgt_sents: list of gold-standard target sentences, wrapped by `<s>` and `</s>`

        Returns:
            scores: could be a variable of shape (batch_size, ) representing the 
                log-likelihood of generating the gold-standard target sentence for 
                each example in the input batch
        """
        tgt_input,tgt_target = tgt_sents
        loss = self.loss
        decoder_outputs, decoder_hidden,symbols = self.decoder(tgt_input, decoder_init_state, src_encodings) 
        loss.reset()
        for step, step_output in enumerate(decoder_outputs):
            batch_size = tgt_input.size(0)
            loss.eval_batch(step_output.contiguous().view(batch_size, -1), tgt_target[:, step])
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), 5.0)
        torch.nn.utils.clip_grad_norm_(self.decoder.parameters(), 5.0)
        self.optimizer.step()
        scores = loss.get_loss()

        return scores, symbols


    def decode_without_bp(self, src_encodings, decoder_init_state, tgt_sents):
        """
        Given source encodings, compute the log-likelihood of predicting the gold-standard target
        sentence tokens

        Args:
            src_encodings: hidden states of tokens in source sentences
            decoder_init_state: decoder GRU/LSTM's initial state
            tgt_sents: list of gold-standard target sentences, wrapped by `<s>` and `</s>`

        Returns:
            scores: could be a variable of shape (batch_size, ) representing the
                log-likelihood of generating the gold-standard target sentence for
                each example in the input batch
        """
        tgt_input,tgt_target = tgt_sents
        loss = self.loss
        decoder_outputs, decoder_hidden, symbols = self.decoder(tgt_input, decoder_init_state, src_encodings, stage="valid")
        loss.reset()
        for step, step_output in enumerate(decoder_outputs):
            batch_size = tgt_input.size(0)
            loss.eval_batch(step_output.contiguous().view(batch_size, -1), tgt_target[:, step])

        scores = loss.get_loss()

        return scores, symbols


    # TODO: sent_padding for only src
    # def beam_search(self, src_sent: List[str], beam_size: int=5, max_decoding_time_step: int=70) -> List[Hypothesis]:
    def beam_search(self, src_sent, beam_size, max_decoding_time_step):
        """
        Given a single source sentence, perform beam search

        Args:
            src_sent: a single tokenized source sentence
            beam_size: beam size
            max_decoding_time_step: maximum number of time steps to unroll the decoding RNN

        Returns:
            hypotheses: a list of hypothesis, each hypothesis has two fields:
                value: List[str]: the decoded target sentence, represented as a list of words
                score: float: the log-likelihood of the target sentence
        """

        hypotheses = 0
        return hypotheses
    
    # def evaluate_ppl(self, dev_data: List[Any], batch_size: int=32):
    def evaluate_ppl(self, dev_data, batch_size):
        """
        Evaluate perplexity on dev sentences

        Args:
            dev_data: a list of dev sentences
            batch_size: batch size
        
        Returns:
            ppl: the perplexity on dev sentences
        """

        """
        cum_loss = 0.
        count = 0

        ref_corpus = []
        hyp_corpus = []
        for src_sents, tgt_sents in batch_iter(dev_data, batch_size):
            ref_corpus.extend(tgt_sents)
            src_sents = self.vocab.src.words2indices(src_sents)
            tgt_sents = self.vocab.tgt.words2indices(tgt_sents)
            src_sents, src_len, y_input, y_tgt, tgt_len = sent_padding(src_sents, tgt_sents)
            src_encodings, decoder_init_state = self.encode(src_sents, src_len)
            decoder_outputs, loss = self.decode_without_bp(src_encodings, decoder_init_state, [y_input, y_tgt])
            cum_loss += loss
            count += 1

            # decoder outputs to word sequence
            hyp_np = np.zeros((len(tgt_sents), len(decoder_outputs), len(self.vocab.tgt)))

            for step in range(len(decoder_outputs)):
                tmp = decoder_outputs[step].cpu().data.numpy()
                # print(tmp.shape)
                hyp_np[:, step, :] = tmp
            # print(hyp_np.shape)

            # converting softmax to word string
            for b in range(hyp_np.shape[0]):
                word_seq = []
                for step in range(hyp_np.shape[1]):
                    pred_idx = np.argmax(hyp_np[b, step, :])
                    # print(pred_idx)
                    if pred_idx == self.vocab.tgt.word2id['</s>']:
                        break
                    word_seq.append(self.vocab.tgt.id2word[pred_idx])
                hyp_corpus.append(word_seq)

            # tgt_word_num_to_predict = sum(len(s[1:]) for s in tgt_sents)  # omitting the leading `<s>`
            # cum_tgt_words += tgt_word_num_to_predict

        # ppl = np.exp(cum_loss / cum_tgt_words)
        for r, h in zip(ref_corpus, hyp_corpus):
            print(r)
            print(h)
            print()
        bleu = compute_corpus_level_bleu_score(ref_corpus, hyp_corpus)
        print('bleu score: ', bleu)

        return cum_loss / count
        """

        ref_corpus = []
        hyp_corpus = []
        cum_loss = 0
        count = 0
        hyp_corpus_ordered = []
        with torch.no_grad():
            for src_sents, tgt_sents, orig_indices in batch_iter(dev_data, batch_size):
                ref_corpus.extend(tgt_sents)
                actual_size = len(src_sents)
                src_sents = self.vocab.src.words2indices(src_sents)
                tgt_sents = self.vocab.tgt.words2indices(tgt_sents)
                src_sents, src_len, y_input, y_tgt, tgt_len = sent_padding(src_sents, tgt_sents)
                src_encodings, decoder_init_state = self.encode(src_sents,src_len)
                scores, symbols = self.decode_without_bp(src_encodings, decoder_init_state, [y_input, y_tgt])
                #sents = np.zeros((len(symbols),actual_size))
                #for i,symbol in enumerate(symbols):
                #    sents[i,:] = symbol.data.cpu().numpy()
                    # print(sents.T)

                index = 0
                batch_hyp_orderd = [None] * symbols.size(0)
                for sent in symbols:

                    word_seq = []
                    for idx in sent:
                        if idx == 2:
                            break
                        word_seq.append(self.vocab.tgt.id2word[np.asscalar(idx)])
                    hyp_corpus.append(word_seq)
                    batch_hyp_orderd[orig_indices[index]] = word_seq
                    index += 1
                hyp_corpus_ordered.extend(batch_hyp_orderd)
                cum_loss += scores
                count += 1
        with open('decode.txt', 'a') as f:
            for r, h in zip(ref_corpus, hyp_corpus_ordered):
                f.write(" ".join(h) + '\n')
        bleu = compute_corpus_level_bleu_score(ref_corpus, hyp_corpus)
        print('bleu score: ', bleu)
        
        return cum_loss / count

    # @staticmethod
    def load(self, model_path):

        self.encoder.load_state_dict(torch.load(model_path + '-encoder'))
        self.decoder.load_state_dict(torch.load(model_path + '-decoder'))
        self.encoder.eval()
        self.decoder.eval()

    def save(self, model_save_path):
        """
        Save current model to file
        """
        torch.save(self.encoder.state_dict(), model_save_path + '-encoder')
        torch.save(self.decoder.state_dict(), model_save_path + '-decoder')


def to_cuda(tensor):
    # Tensor -> Variable (on GPU if possible)
    if torch.cuda.is_available():
    # Tensor -> GPU Tensor
        tensor = tensor.cuda()
    return tensor


def sent_padding(src_sents, tgt_sents):
    batch_size = len(src_sents)

    max_src_len = max([len(sent) for sent in src_sents])
    max_tgt_len = max([len(sent) for sent in tgt_sents])

    padded_src_sents = np.zeros((batch_size, max_src_len))
    padded_Yinput = np.zeros((batch_size, max_tgt_len))
    padded_Ytarget = np.zeros((batch_size, max_tgt_len))

    src_lens = []
    tgt_lens = []

    for i, sent in enumerate(zip(src_sents, tgt_sents)):
        src_sent = sent[0]
        y_input = sent[1][:-1]
        y_target = sent[1][1:]

        src_len = len(src_sent)
        tgt_len = len(y_input)

        padded_src_sents[i, :src_len] = src_sent
        padded_Yinput[i, :tgt_len] = y_input
        padded_Ytarget[i, :tgt_len] = y_target

        src_lens.append(src_len)
        tgt_lens.append(tgt_len)

    return to_cuda(torch.LongTensor(padded_src_sents)), src_lens, \
           to_cuda(torch.LongTensor(padded_Yinput)), to_cuda(torch.LongTensor(padded_Ytarget)), tgt_lens


# def compute_corpus_level_bleu_score(references: List[List[str]], hypotheses: List[Hypothesis]) -> float:
def compute_corpus_level_bleu_score(references, hypotheses):
    """
    Given decoding results and reference sentences, compute corpus-level BLEU score

    Args:
        references: a list of gold-standard reference target sentences
        hypotheses: a list of hypotheses, one for each reference

    Returns:
        bleu_score: corpus-level BLEU score
    """
    if references[0][0] == '<s>':
        references = [ref[1:-1] for ref in references]

    bleu_score = corpus_bleu([[ref] for ref in references], hypotheses)

    # bleu_score = corpus_bleu([[ref] for ref in references],
    #                          [hyp.value for hyp in hypotheses])

    return bleu_score


# def train(args: Dict[str, str]):
def train(args):
    train_data_src = read_corpus(args['--train-src'], source='src')
    train_data_tgt = read_corpus(args['--train-tgt'], source='tgt')

    dev_data_src = read_corpus(args['--dev-src'], source='src')
    dev_data_tgt = read_corpus(args['--dev-tgt'], source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))

    train_batch_size = int(args['--batch-size'])
    clip_grad = float(args['--clip-grad'])
    valid_niter = int(args['--valid-niter'])
    log_every = int(args['--log-every'])
    # model_save_path = args['--save-to']
    model_save_path = 'model'
    #valid_niter = 100

    vocab = pickle.load(open(args['--vocab'], 'rb'))

    model = NMT(embed_size=int(args['--embed-size']),
                hidden_size=int(args['--hidden-size']),
                dropout_rate=float(args['--dropout']),
                vocab=vocab,keep_train=True)

    num_trial = 0
    train_iter = patience = cum_loss = report_loss = cumulative_tgt_words = report_tgt_words = 0
    cumulative_examples = report_examples = epoch = valid_num = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin Maximum Likelihood training')

    # train_iter = -1

    while True:
        epoch += 1

        for src_sents, tgt_sents, _ in batch_iter(train_data, batch_size=train_batch_size, shuffle=True):
            train_iter += 1

            batch_size = len(src_sents)

            # (batch_size)
            loss = model(src_sents, tgt_sents)

            report_loss += loss
            cum_loss += loss

            tgt_words_num_to_predict = sum(len(s[1:]) for s in tgt_sents)  # omitting leading `<s>`
            report_tgt_words += tgt_words_num_to_predict
            cumulative_tgt_words += tgt_words_num_to_predict
            report_examples += batch_size
            cumulative_examples += batch_size

            if train_iter % log_every == 0:
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         math.exp(report_loss / report_tgt_words),
                                                                                         cumulative_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.

            # the following code performs validation on dev set, and controls the learning schedule
            # if the dev score is better than the last check point, then the current model is saved.
            # otherwise, we allow for that performance degeneration for up to `--patience` times;
            # if the dev score does not increase after `--patience` iterations, we reload the previously
            # saved best model (and the state of the optimizer), halve the learning rate and continue
            # training. This repeats for up to `--max-num-trial` times.

            if train_iter % valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cumulative_examples,
                                                                                         np.exp(cum_loss / cumulative_tgt_words),
                                                                                         cumulative_examples), file=sys.stderr)

                cum_loss = cumulative_examples = cumulative_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)

                # compute dev. ppl and bleu
                dev_ppl = model.evaluate_ppl(dev_data, batch_size=128)   # dev batch size can be a bit larger
                valid_metric = -dev_ppl

                print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl), file=sys.stderr)

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                hist_valid_scores.append(valid_metric)

                if is_better:
                    patience = 0
                    print('save currently the best model to [%s]' % model_save_path, file=sys.stderr)
                    model.save(model_save_path)

                    # You may also save the optimizer's state
                elif patience < int(args['--patience']):
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)

                    if patience == int(args['--patience']):
                        num_trial += 1
                        print('hit #%d trial' % num_trial, file=sys.stderr)
                        if num_trial == int(args['--max-num-trial']):
                            print('early stop!', file=sys.stderr)
                            exit(0)

                        # decay learning rate, and restore from previously best checkpoint
                        model.scheduler.step()
                        print('load previously best model and decay learning rate by half', file=sys.stderr)

                        # load model
                        model.load(model_save_path)

                        print('restore parameters of the optimizers', file=sys.stderr)
                        # You may also need to load the state of the optimizer saved before

                        # reset patience
                        patience = 0

                if epoch == int(args['--max-epoch']):
                    print('reached maximum number of epochs!', file=sys.stderr)
                    exit(0)



def beam_search(model: NMT, test_data_src: List[List[str]], beam_size: int, max_decoding_time_step: int) -> List[List[Hypothesis]]:
    was_training = model.training

    hypotheses = []
    for src_sent in tqdm(test_data_src, desc='Decoding', file=sys.stdout):
        example_hyps = model.beam_search(src_sent, beam_size=beam_size, max_decoding_time_step=max_decoding_time_step)

        hypotheses.append(example_hyps)

    return hypotheses


def decode(args: Dict[str, str]):
    """
    performs decoding on a test set, and save the best-scoring decoding results. 
    If the target gold-standard sentences are given, the function also computes
    corpus-level BLEU score.
    """
    test_data_src = read_corpus(args['TEST_SOURCE_FILE'], source='src')
    if args['TEST_TARGET_FILE']:
        test_data_tgt = read_corpus(args['TEST_TARGET_FILE'], source='tgt')

    print(f"load model from {args['MODEL_PATH']}", file=sys.stderr)
    model = NMT.load(args['MODEL_PATH'])

    hypotheses = beam_search(model, test_data_src,
                             beam_size=int(args['--beam-size']),
                             max_decoding_time_step=int(args['--max-decoding-time-step']))

    if args['TEST_TARGET_FILE']:
        top_hypotheses = [hyps[0] for hyps in hypotheses]
        bleu_score = compute_corpus_level_bleu_score(test_data_tgt, top_hypotheses)
        print(f'Corpus BLEU: {bleu_score}', file=sys.stderr)

    with open(args['OUTPUT_FILE'], 'w') as f:
        for src_sent, hyps in zip(test_data_src, hypotheses):
            top_hyp = hyps[0]
            hyp_sent = ' '.join(top_hyp.value)
            f.write(hyp_sent + '\n')


def main():
    args = docopt(__doc__)

    # seed the random number generator (RNG), you may
    # also want to seed the RNG of tensorflow, pytorch, dynet, etc.
    seed = int(args['--seed'])
    np.random.seed(seed * 13 // 7)

    if args['train']:
        train(args)
    elif args['decode']:
        decode(args)
    else:
        raise RuntimeError(f'invalid mode')


if __name__ == '__main__':
    main()
