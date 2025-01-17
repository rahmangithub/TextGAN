import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import functools
import itertools
import config as cfg
from instructor.real_data.instructor import BasicInstructor
from metrics.bleu import BLEU
from models.RelbarGAN_D import RelbarGAN_D
from models.RelbarGAN_G import RelbarGAN_G
from utils.rebar_gradient_estimator import RebarGradientEstimator
from utils.data_loader import GenDataIter, DisDataIter
from utils.helpers import get_fixed_temperature, get_losses
from utils.text_process import tensor_to_tokens


class RelbarGANInstructor(BasicInstructor):
    def __init__(self, opt):
        super(RelbarGANInstructor, self).__init__(opt)

        # generator, discriminator
        self.gen = RelbarGAN_G(cfg.mem_slots, cfg.num_heads, cfg.head_size, cfg.gen_embed_dim, cfg.gen_hidden_dim,
                            cfg.vocab_size, cfg.max_seq_len, cfg.padding_idx, cfg.temperature, cfg.eta, gpu=cfg.CUDA)
        self.dis = RelbarGAN_D(cfg.dis_embed_dim, cfg.max_seq_len, cfg.num_rep, cfg.vocab_size, cfg.padding_idx,
                            gpu=cfg.CUDA)
        self.init_model()

        # Optimizer
        self.gen_opt = optim.Adam(self.gen.parameters(), lr=cfg.gen_lr)
        self.gen_adv_opt = optim.Adam(itertools.chain(self.gen.parameters(), [self.gen.temperature, self.gen.eta]),
                                      lr=cfg.gen_adv_lr)
        self.dis_opt = optim.Adam(self.dis.parameters(), lr=cfg.dis_lr)

        # Criterion
        self.mle_criterion = nn.NLLLoss()
        self.dis_pretrain_criterion = nn.BCEWithLogitsLoss()

        # DataLoader
        self.gen_data = GenDataIter(self.gen.sample(cfg.batch_size, cfg.batch_size))
        self.dis_data = DisDataIter(self.train_data.random_batch()['target'], self.gen_data.random_batch()['target'])

        # Metrics
        bleu_gram = list(range(2, cfg.max_seq_len + 1)) if cfg.max_seq_len < 5 else [2, 3, 4, 5]
        self.bleu = BLEU(test_text=tensor_to_tokens(self.gen_data.target, self.index_word_dict),
                         real_text=tensor_to_tokens(self.test_data.target, self.test_data.index_word_dict),
                         gram=bleu_gram)
        self.self_bleu = BLEU(test_text=tensor_to_tokens(self.gen_data.target, self.index_word_dict),
                              real_text=tensor_to_tokens(self.gen_data.target, self.index_word_dict),
                              gram=3)

    def _run(self):
        # =====PRE-TRAINING (GENERATOR)=====
        if not cfg.gen_pretrain:
            self.log.info('Starting Generator MLE Training...')
            self.pretrain_generator(cfg.MLE_train_epoch)
            if cfg.if_save and not cfg.if_test:
                torch.save(self.gen.state_dict(), cfg.pretrained_gen_path)
                print('Save pretrain_generator: {}'.format(cfg.pretrained_gen_path))

        # =====PRE-TRAINING (DISCRIMINATOR)=====
        if not cfg.dis_pretrain:
            self.log.info('Starting Discriminator Training...')
            self.pretrain_discriminator(cfg.d_step, cfg.d_epoch)
            if cfg.if_save and not cfg.if_test:
                torch.save(self.dis.state_dict(), cfg.pretrained_dis_path)
                print('Save pretrain_generator discriminator: {}'.format(cfg.pretrained_dis_path))

        # # =====ADVERSARIAL TRAINING=====
        self.log.info('Starting Adversarial Training...')
        self.log.info('Initial generator: %s' % (self.cal_metrics(fmt_str=True)))

        for adv_epoch in range(cfg.ADV_train_epoch):
            if adv_epoch % cfg.adv_log_step == 0:
                self.log.info('-----\nADV EPOCH %d\n-----' % adv_epoch)
            self.sig.update()
            if self.sig.adv_sig:
                self.adv_train_generator(cfg.ADV_g_step, adv_epoch)  # Generator
                self.adv_train_discriminator(cfg.ADV_d_step, adv_epoch)  # Discriminator

                if adv_epoch % cfg.adv_log_step == 0:
                    if cfg.if_save and not cfg.if_test:
                        self._save('ADV', adv_epoch)
            else:
                self.log.info('>>> Stop by adv_signal! Finishing adversarial training...')
                break

    def _test(self):
        print('>>> Begin test...')

        self._run()
        pass

    def pretrain_generator(self, epochs):
        """
        Max Likelihood Pre-training for the generator
        """
        for epoch in range(epochs):
            self.sig.update()
            if self.sig.pre_sig:
                # =====Train=====
                pre_loss = self.train_gen_epoch(self.gen, self.train_data.loader, self.mle_criterion, self.gen_opt)

                # =====Test=====
                if epoch % cfg.pre_log_step == 0 or epoch == epochs - 1:
                    self.log.info('[MLE-GEN] epoch %d : pre_loss = %.4f, %s' % (
                        epoch, pre_loss, self.cal_metrics(fmt_str=True)))

                    if cfg.if_save and not cfg.if_test:
                        self._save('MLE', epoch)
            else:
                self.log.info('>>> Stop by pre signal, skip to adversarial training...')
                break
        if cfg.if_save and not cfg.if_test:
            self._save('MLE', epoch)

    def adv_train_generator(self, g_step, adv_epoch):
        rebar_ge = RebarGradientEstimator(discriminator=self.dis, batch_size=cfg.batch_size,
                                          real_samples=self.train_data.random_batch()['target'],
                                          gpu=cfg.CUDA, num_rep=cfg.num_rep)
        total_rebar_loss = 0
        old_temperature = self.gen.temperature.item()
        old_eta = self.gen.eta.item()
        for step in range(g_step):
            theta, z = self.gen.sample_theta(cfg.batch_size)
            estimated_gradient, temperature_grad, eta_grad = rebar_ge.estimate_gradient(theta, z,
                                                                                        self.gen.temperature.clone().detach().requires_grad_(),
                                                                                        self.gen.eta.clone().detach().requires_grad_())

            adv_loss = self.gen.computeRebarLoss(estimated_gradient)
            temperature_grad = temperature_grad if cfg.learn_temperature else torch.zeros_like(temperature_grad)
            eta_grad = eta_grad if cfg.learn_eta else torch.zeros_like(eta_grad)
            self.optimize(self.gen_adv_opt, adv_loss,
                          callback=functools.partial(self.gen.set_variance_loss_gradients, temperature_grad, eta_grad))
            total_rebar_loss += adv_loss.item()

        # =====Test=====
        avg_rebar_loss = total_rebar_loss / g_step if g_step != 0 else 0
        if adv_epoch % cfg.adv_log_step == 0:
            self.log.info('[ADV-GEN] rebar_loss = %.4f, temperature = %.4f, eta = %.4f, %s'
                      % (avg_rebar_loss, old_temperature, old_eta, self.cal_metrics(fmt_str=True)))

    def adv_train_discriminator(self, d_step, adv_epoch):
        total_loss = 0
        total_acc = 0
        for step in range(d_step):
            # TODO(ethanjiang) we may want to train a full epoch instead of a random batch
            real_samples = self.train_data.random_batch()['target']
            gen_samples = self.gen.sample(cfg.batch_size, cfg.batch_size)
            if cfg.CUDA:
                real_samples, gen_samples = real_samples.cuda(), gen_samples.cuda()
            real_samples = F.one_hot(real_samples, cfg.vocab_size).float()
            gen_samples = F.one_hot(gen_samples, cfg.vocab_size).float()

            # =====Train=====
            d_out_real = self.dis(real_samples)
            d_out_fake = self.dis(gen_samples)
            _, d_loss = get_losses(d_out_real, d_out_fake, cfg.loss_type)

            self.dis_opt.zero_grad()
            d_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.dis.parameters(), cfg.clip_norm)
            self.dis_opt.step()

            total_loss += d_loss.item()
            if cfg.loss_type != 'rsgan':
                predictions = torch.cat((d_out_real, d_out_fake))
                labels = torch.cat((torch.ones_like(d_out_real), torch.zeros_like(d_out_fake)))
                total_acc += torch.sum(((predictions > 0).float() == labels)).item()

        # =====Test=====
        avg_loss = total_loss / d_step if d_step != 0 else 0
        if cfg.loss_type != 'rsgan':
            avg_acc = total_acc / (d_step * cfg.batch_size * 2) if d_step != 0 else 0
            if adv_epoch % cfg.adv_log_step == 0:
                self.log.info('[ADV-DIS] d_loss = %.4f, train_acc = %.4f,' % (avg_loss, avg_acc))
        else:
            if adv_epoch % cfg.adv_log_step == 0:
                self.log.info('[ADV-DIS] d_loss = %.4f,' % (avg_loss))

    def pretrain_discriminator(self, d_step, d_epoch, phrase='MLE'):
        """
        Training the discriminator on real_data_samples (positive) and generated samples from gen (negative).
        Samples are drawn d_step times, and the discriminator is trained for d_epoch d_epoch.
        """
        # prepare loader for validate
        for step in range(d_step):
            # prepare loader for training
            pos_samples = self.train_data.target
            neg_samples = self.gen.sample(cfg.samples_num, 4 * cfg.batch_size)
            self.dis_data.reset(pos_samples, neg_samples)

            for epoch in range(d_epoch):
                # =====Train=====
                d_loss, train_acc = self.train_dis_epoch(self.dis, self.dis_data.loader, self.dis_pretrain_criterion,
                                                         self.dis_opt, one_hot=True, num_rep=cfg.num_rep)

            # =====Test=====
            self.log.info('[%s-DIS] d_step %d: d_loss = %.4f, train_acc = %.4f,' % (
                phrase, step, d_loss, train_acc))
