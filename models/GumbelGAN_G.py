import torch
import torch.nn.functional as F

import config as cfg
from models.generator import LSTMGenerator


class GumbelGAN_G(LSTMGenerator):
    def __init__(self, embedding_dim, hidden_dim, vocab_size, max_seq_len, padding_idx, gpu=False):
        super(GumbelGAN_G, self).__init__(embedding_dim, hidden_dim, vocab_size, max_seq_len, padding_idx, gpu)

        self.name = 'gumbelgan'
        self.temperature = 1.0  # init value is 1.0
        self.init_params()

    def step(self, inp, hidden):
        """
        GumbelGAN step forward
        :param inp: [batch_size]
        :param hidden: (h, c)
        :return: pred, hidden, next_token, next_token_onehot
            - pred: batch_size * vocab_size, use for adversarial training backward
            - hidden: next hidden
            - next_token: [batch_size], next sentence token
            - next_token_onehot: batch_size * vocab_size, not used yet
        """
        emb = self.embeddings(inp).unsqueeze(1)
        out, hidden = self.lstm(emb, hidden)
        gumbel_t = self.add_gumbel(self.lstm2out(out.squeeze(1)))
        next_token = torch.argmax(gumbel_t, dim=1).detach()
        # next_token_onehot = F.one_hot(next_token, cfg.vocab_size).float()  # not used yet
        next_token_onehot = None

        pred = F.softmax(gumbel_t * self.temperature, dim=-1)  # batch_size * vocab_size

        return pred, hidden, next_token, next_token_onehot

    def sample(self, num_samples, batch_size, one_hot=False, start_letter=cfg.start_letter):
        """
        Sample from GumbelGAN Generator
        - one_hot: if return pred of GumbelGAN, used for adversarial training
        :return:
            - all_preds: batch_size * seq_len * vocab_size, only use for a batch
            - samples: all samples
        """
        num_batch = num_samples // batch_size + 1 if num_samples != batch_size else 1
        samples = torch.zeros(num_batch * batch_size, self.max_seq_len).long()
        if one_hot:
            all_preds = torch.zeros(batch_size, self.max_seq_len, self.vocab_size)
            if self.gpu:
                all_preds = all_preds.cuda()

        for b in range(num_batch):
            hidden = self.init_hidden(batch_size)
            inp = torch.LongTensor([start_letter] * batch_size)
            if self.gpu:
                inp = inp.cuda()

            for i in range(self.max_seq_len):
                pred, hidden, next_token, _ = self.step(inp, hidden)
                samples[b * batch_size:(b + 1) * batch_size, i] = next_token
                if one_hot:
                    all_preds[:, i] = pred
                inp = next_token
        samples = samples[:num_samples]  # num_samples * seq_len

        if one_hot:
            return all_preds  # batch_size * seq_len * vocab_size
        return samples


    @staticmethod
    def add_gumbel(theta_logit, eps=1e-10, gpu=cfg.CUDA):
        u = torch.zeros(theta_logit.size())
        if gpu:
            u = u.cuda()

        u.uniform_(0, 1)
        # F.softmax(theta_logit, dim=-1) converts theta_logit to categorical distribution.
        gumbel_t = torch.log(F.softmax(theta_logit, dim=-1) + eps) - torch.log(-torch.log(u + eps) + eps)
        # gumbel_t = theta_logit - torch.log(-torch.log(u + eps) + eps)  TODO(ethanjiang) should come back to this formula and evaluate again
        return gumbel_t
