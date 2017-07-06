# Copyright 2017 Google Inc. All Rights Reserved.
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
# ==============================================================================

"""For training NMT models."""
from __future__ import print_function

import math
import os
import random
import time

import tensorflow as tf

from tensorflow.python.ops import lookup_ops

from . import attention_model
from . import gnmt_model
from . import inference
from . import model as nmt_model
from . import model_helper
from .utils import iterator_utils
from .utils import misc_utils as utils
from .utils import nmt_utils
from .utils import vocab_utils

utils.check_tensorflow_version()

__all__ = ["train"]


def create_train_model(model_creator,
                       hparams,
                       scope=None):
  """Create train graph, model, and iterator."""
  train_src_file = "%s.%s" % (hparams.train_prefix, hparams.src)
  train_tgt_file = "%s.%s" % (hparams.train_prefix, hparams.tgt)
  src_vocab_file = hparams.src_vocab_file
  tgt_vocab_file = hparams.tgt_vocab_file

  train_graph = tf.Graph()

  with train_graph.as_default():
    src_vocab_table = lookup_ops.index_table_from_file(
        src_vocab_file, default_value=vocab_utils.UNK_ID)
    tgt_vocab_table = lookup_ops.index_table_from_file(
        tgt_vocab_file, default_value=vocab_utils.UNK_ID)

    train_src_dataset = tf.contrib.data.TextLineDataset(train_src_file)
    train_tgt_dataset = tf.contrib.data.TextLineDataset(train_tgt_file)
    train_skip_count_placeholder = tf.placeholder(shape=(), dtype=tf.int64)

    train_iterator = iterator_utils.get_iterator(
        train_src_dataset,
        train_tgt_dataset,
        src_vocab_table,
        tgt_vocab_table,
        batch_size=hparams.batch_size,
        sos=hparams.sos,
        eos=hparams.eos,
        source_reverse=hparams.source_reverse,
        random_seed=hparams.random_seed,
        num_buckets=hparams.num_buckets,
        src_max_len=hparams.src_max_len,
        tgt_max_len=hparams.tgt_max_len,
        skip_count=train_skip_count_placeholder)
    train_model = model_creator(
        hparams,
        iterator=train_iterator,
        mode=tf.contrib.learn.ModeKeys.TRAIN,
        source_vocab_table=src_vocab_table,
        target_vocab_table=tgt_vocab_table,
        scope=scope)

  return train_graph, train_model, train_iterator, train_skip_count_placeholder


def create_eval_model(model_creator,
                      hparams,
                      scope=None):
  """Create train graph, model, src/tgt file holders, and iterator."""
  src_vocab_file = hparams.src_vocab_file
  tgt_vocab_file = hparams.tgt_vocab_file
  eval_graph = tf.Graph()

  with eval_graph.as_default():
    src_vocab_table = lookup_ops.index_table_from_file(
        src_vocab_file, default_value=vocab_utils.UNK_ID)
    tgt_vocab_table = lookup_ops.index_table_from_file(
        tgt_vocab_file, default_value=vocab_utils.UNK_ID)
    eval_src_file_placeholder = tf.placeholder(shape=(), dtype=tf.string)
    eval_tgt_file_placeholder = tf.placeholder(shape=(), dtype=tf.string)
    eval_src_dataset = tf.contrib.data.TextLineDataset(
        eval_src_file_placeholder)
    eval_tgt_dataset = tf.contrib.data.TextLineDataset(
        eval_tgt_file_placeholder)
    eval_iterator = iterator_utils.get_iterator(
        eval_src_dataset,
        eval_tgt_dataset,
        src_vocab_table,
        tgt_vocab_table,
        hparams.batch_size,
        sos=hparams.sos,
        eos=hparams.eos,
        source_reverse=hparams.source_reverse,
        random_seed=hparams.random_seed,
        num_buckets=hparams.num_buckets,
        src_max_len=hparams.src_max_len_infer,
        tgt_max_len=hparams.tgt_max_len_infer)
    eval_model = model_creator(
        hparams,
        iterator=eval_iterator,
        mode=tf.contrib.learn.ModeKeys.EVAL,
        source_vocab_table=src_vocab_table,
        target_vocab_table=tgt_vocab_table,
        scope=scope)
  return (eval_graph, eval_model, eval_src_file_placeholder,
          eval_tgt_file_placeholder, eval_iterator)


def train(hparams, eval_only=False, scope=None):
  """Train a translation model."""
  log_device_placement = hparams.log_device_placement
  out_dir = hparams.out_dir
  num_train_steps = hparams.num_train_steps
  steps_per_stats = hparams.steps_per_stats
  steps_per_external_eval = hparams.steps_per_external_eval
  steps_per_eval = 10 * steps_per_stats
  if not steps_per_external_eval:
    steps_per_external_eval = 5 * steps_per_eval

  dev_src_file = "%s.%s" % (hparams.dev_prefix, hparams.src)
  dev_tgt_file = "%s.%s" % (hparams.dev_prefix, hparams.tgt)
  if hparams.test_prefix:
    test_src_file = "%s.%s" % (hparams.test_prefix, hparams.src)
    test_tgt_file = "%s.%s" % (hparams.test_prefix, hparams.tgt)

  if not hparams.attention:
    model_creator = nmt_model.Model
  elif hparams.attention_architecture == "standard":
    model_creator = attention_model.AttentionModel
  elif hparams.attention_architecture in ["gnmt", "gnmt_v2"]:
    model_creator = gnmt_model.GNMTModel
  else:
    raise ValueError("Unknown model architecture")

  (train_graph, train_model, train_iterator, train_skip_count_placeholder) = (
      create_train_model(model_creator, hparams, scope))

  (eval_graph, eval_model, eval_src_file_placeholder,
   eval_tgt_file_placeholder, eval_iterator) = create_eval_model(
       model_creator, hparams, scope)
  (infer_graph, infer_model, infer_src_placeholder,
   infer_batch_size_placeholder,
   infer_iterator) = (inference.create_infer_model(
       model_creator, hparams, scope))

  dev_eval_iterator_feed_dict = {
      eval_src_file_placeholder: dev_src_file,
      eval_tgt_file_placeholder: dev_tgt_file
  }
  dev_src_data = inference.load_data(dev_src_file)
  dev_tgt_data = inference.load_data(dev_tgt_file)
  dev_infer_iterator_feed_dict = {
      infer_src_placeholder: dev_src_data,
      infer_batch_size_placeholder: hparams.infer_batch_size,
  }
  if hparams.test_prefix:
    test_eval_iterator_feed_dict = {
        eval_src_file_placeholder: test_src_file,
        eval_tgt_file_placeholder: test_tgt_file
    }
    test_infer_iterator_feed_dict = {
        infer_src_placeholder: inference.load_data(test_src_file),
        infer_batch_size_placeholder: hparams.infer_batch_size,
    }

  if eval_only:
    if len(hparams.metrics) != 1:
      raise ValueError("Expect exactly 1 metric in eval_only mode.")

    summary_name = "eval_log"
    model_dir = getattr(hparams, "best_" + hparams.metrics[0] + "_dir")
  else:
    summary_name = "train_log"
    model_dir = hparams.out_dir

  # Log and output files
  log_file = os.path.join(out_dir, "log_%d" % time.time())
  log_f = tf.gfile.GFile(log_file, mode="a")
  utils.print_out("# log_file=%s" % log_file, log_f)

  avg_step_time = 0.0

  # TensorFlow model
  config_proto = utils.get_config_proto(
      log_device_placement=log_device_placement)

  train_sess = tf.Session(config=config_proto, graph=train_graph)
  eval_sess = tf.Session(config=config_proto, graph=eval_graph)
  infer_sess = tf.Session(config=config_proto, graph=infer_graph)

  with train_graph.as_default():
    train_model, global_step = model_helper.create_or_load_model(
        train_model, model_dir, train_sess, hparams.out_dir, "train")

  # Summary writer
  summary_writer = tf.summary.FileWriter(
      os.path.join(out_dir, summary_name), train_graph)

  # For internal evaluation (perplexity)
  def run_internal_eval():
    """Compute internal evaluation for both dev / test."""
    with infer_graph.as_default():
      loaded_infer_model, global_step = model_helper.create_or_load_model(
          infer_model, model_dir, infer_sess, hparams.out_dir, "infer")

    _sample_decode(loaded_infer_model, global_step, infer_sess, hparams,
                   infer_iterator, dev_src_data, dev_tgt_data,
                   infer_src_placeholder, infer_batch_size_placeholder,
                   summary_writer)

    with eval_graph.as_default():
      loaded_eval_model, global_step = model_helper.create_or_load_model(
          eval_model, model_dir, eval_sess, hparams.out_dir, "eval")

    dev_ppl = _internal_eval(loaded_eval_model, global_step, eval_sess,
                             eval_iterator, dev_eval_iterator_feed_dict,
                             summary_writer, "dev")
    test_ppl = None
    if hparams.test_prefix:
      test_ppl = _internal_eval(loaded_eval_model, global_step, eval_sess,
                                eval_iterator, test_eval_iterator_feed_dict,
                                summary_writer, "test")
    return dev_ppl, test_ppl

  # For external evaluation (bleu, rouge, etc.)
  def run_external_eval():
    """Compute external evaluation for both dev / test."""
    with infer_graph.as_default():
      loaded_infer_model, global_step = model_helper.create_or_load_model(
          infer_model, model_dir, infer_sess, hparams.out_dir, "infer")

    _sample_decode(loaded_infer_model, global_step, infer_sess, hparams,
                   infer_iterator, dev_src_data, dev_tgt_data,
                   infer_src_placeholder, infer_batch_size_placeholder,
                   summary_writer)

    dev_scores = _external_eval(
        loaded_infer_model,
        global_step,
        infer_sess,
        hparams,
        infer_iterator,
        dev_infer_iterator_feed_dict,
        dev_tgt_file,
        "dev",
        summary_writer,
        save_on_best=(not eval_only))
    test_scores = None
    if hparams.test_prefix:
      test_scores = _external_eval(
          loaded_infer_model,
          global_step,
          infer_sess,
          hparams,
          infer_iterator,
          test_infer_iterator_feed_dict,
          test_tgt_file,
          "test",
          summary_writer,
          save_on_best=False)
    return dev_scores, test_scores

  # First evaluation
  dev_ppl, test_ppl = run_internal_eval()
  dev_scores, test_scores = run_external_eval()

  all_dev_perplexities = [dev_ppl]
  all_test_perplexities = [test_ppl]
  all_steps = [global_step]

  # This is the training loop.
  step_time, checkpoint_loss, checkpoint_predict_count = 0.0, 0.0, 0.0
  checkpoint_total_count = 0.0
  speed, train_ppl = 0.0, 0.0
  start_train_time = time.time()

  utils.print_out(
      "# Start step %d, lr %g, %s" %
      (global_step, train_model.learning_rate.eval(session=train_sess),
       time.ctime()),
      log_f)

  # Initialize all of the iterators
  skip_count = hparams.batch_size * hparams.epoch_step
  utils.print_out("# Init train iterator, skipping %d elements" % skip_count)
  train_sess.run(
      train_iterator.initializer,
      feed_dict={
          train_skip_count_placeholder: skip_count
      })

  while global_step < num_train_steps and not eval_only:
    ### Run a step ###
    start_time = time.time()
    try:
      step_result = train_model.train(train_sess)
      (_, step_loss, step_predict_count, step_summary, global_step,
       step_word_count, batch_size) = step_result
      hparams.epoch_step += 1
    except tf.errors.OutOfRangeError:
      # Finished going through the training dataset.  Go to next epoch.
      hparams.epoch_step = 0
      utils.print_out(
          "# Finished an epoch, step %d. Perform external evaluation" %
          global_step)
      dev_scores, test_scores = run_external_eval()
      train_sess.run(
          train_iterator.initializer,
          feed_dict={
              train_skip_count_placeholder: 0
          })
      continue

    # Write step summary.
    summary_writer.add_summary(step_summary, global_step)

    # update statistics
    step_time += (time.time() - start_time)

    checkpoint_loss += (step_loss * batch_size)
    checkpoint_predict_count += step_predict_count
    checkpoint_total_count += float(step_word_count)

    # Once in a while, we print statistics.
    if global_step % steps_per_stats == 0:
      # Print statistics for the previous epoch.
      avg_step_time = step_time / steps_per_stats
      train_ppl = utils.safe_exp(checkpoint_loss / checkpoint_predict_count)
      speed = checkpoint_total_count / (1000 * step_time)
      utils.print_out(
          "  global step %d lr %g "
          "step-time %.2fs wps %.2fK ppl %.2f %s" %
          (global_step, train_model.learning_rate.eval(session=train_sess),
           avg_step_time, speed, train_ppl, _get_best_results(hparams)),
          log_f)
      if math.isnan(train_ppl): break

      # Reset timer and loss.
      step_time, checkpoint_loss, checkpoint_predict_count = 0.0, 0.0, 0.0
      checkpoint_total_count = 0.0

    if global_step % steps_per_eval == 0:
      utils.print_out("# Save eval, global step %d" % global_step)
      utils.add_summary(summary_writer, global_step, "train_ppl", train_ppl)

      # Save checkpoint
      train_model.saver.save(
          train_sess,
          os.path.join(out_dir, "translate.ckpt"),
          global_step=global_step)

      # Evaluate on dev/test
      dev_ppl, test_ppl = run_internal_eval()

      all_dev_perplexities.append(dev_ppl)
      all_test_perplexities.append(test_ppl)
      all_steps.append(global_step)

    if global_step % steps_per_external_eval == 0:
      # Save checkpoint
      train_model.saver.save(
          train_sess,
          os.path.join(out_dir, "translate.ckpt"),
          global_step=global_step)
      dev_scores, test_scores = run_external_eval()

  # Done training
  if not eval_only:
    train_model.saver.save(
        train_sess,
        os.path.join(out_dir, "translate.ckpt"),
        global_step=global_step)

    dev_ppl, test_ppl = run_internal_eval()
    dev_scores, test_scores = run_external_eval()

  utils.print_out("# Best dev %s" % _get_best_results(hparams))

  all_dev_perplexities.append(dev_ppl)
  all_test_perplexities.append(test_ppl)
  all_steps.append(global_step)

  eval_results = _format_results("dev", dev_ppl, dev_scores, hparams.metrics)
  if hparams.test_prefix:
    eval_results += ", " + _format_results("test", test_ppl, test_scores,
                                           hparams.metrics)

  utils.print_out(
      "# Final, step %d lr %g "
      "step-time %.2f wps %.2fK ppl %.2f, %s, %s" %
      (global_step, train_model.learning_rate.eval(session=train_sess),
       avg_step_time, speed, train_ppl, eval_results, time.ctime()),
      log_f)
  summary_writer.close()
  utils.print_time("# Done training!", start_train_time)

  return all_dev_perplexities, all_test_perplexities, all_steps


def _format_results(name, ppl, scores, metrics):
  """Format results."""
  result_str = "%s ppl %.2f" % (name, ppl)
  if scores:
    for metric in metrics:
      result_str += ", %s %s %.1f" % (name, metric, scores[metric])
  return result_str


def _get_best_results(hparams):
  """Summary of the current best results."""
  tokens = []
  for metric in hparams.metrics:
    tokens.append("%s %.2f" % (metric, getattr(hparams, "best_" + metric)))
  return ", ".join(tokens)


def _internal_eval(model, global_step, sess, iterator, iterator_feed_dict,
                   summary_writer, label):
  """Computing perplexity."""
  sess.run(iterator.initializer, feed_dict=iterator_feed_dict)
  ppl = model_helper.compute_perplexity(model, sess, label)
  utils.add_summary(summary_writer, global_step, "%s_ppl" % label, ppl)
  return ppl


def _sample_decode(model, global_step, sess, hparams, iterator, src_data,
                   tgt_data, iterator_src_placeholder,
                   iterator_batch_size_placeholder, summary_writer):
  """Pick a sentence and decode."""
  decode_id = random.randint(0, len(src_data)-1)
  utils.print_out("  # %d" % decode_id)

  iterator_feed_dict = {
      iterator_src_placeholder: [src_data[decode_id]],
      iterator_batch_size_placeholder: 1,
  }
  sess.run(iterator.initializer, feed_dict=iterator_feed_dict)

  nmt_outputs, attention_summary = model.decode(sess)

  translation = nmt_utils.get_translation(
      nmt_outputs,
      sent_id=0,
      tgt_eos=hparams.eos,
      bpe_delimiter=hparams.bpe_delimiter)
  utils.print_out("    src: %s" % src_data[decode_id])
  utils.print_out("    ref: %s" % tgt_data[decode_id])
  utils.print_out("    nmt: %s" % translation)

  # Summary
  if attention_summary is not None:
    summary_writer.add_summary(attention_summary, global_step)


def _external_eval(model, global_step, sess, hparams, iterator,
                   iterator_feed_dict, tgt_file, label, summary_writer,
                   save_on_best):
  """External evaluation such as BLEU and ROUGE scores."""
  out_dir = hparams.out_dir
  decode = global_step > 0
  if decode:
    utils.print_out("# External evaluation, global step %d" % global_step)

  sess.run(iterator.initializer, feed_dict=iterator_feed_dict)

  output = os.path.join(out_dir, "output_%s" % label)
  scores = nmt_utils.decode_and_evaluate(
      label,
      model,
      sess,
      output,
      ref_file=tgt_file,
      metrics=hparams.metrics,
      bpe_delimiter=hparams.bpe_delimiter,
      beam_width=hparams.beam_width,
      tgt_eos=hparams.eos,
      decode=decode)
  # Save on best metrics
  if decode:
    for metric in hparams.metrics:
      utils.add_summary(summary_writer, global_step, "%s_%s" % (label, metric),
                        scores[metric])
      # metric: larger is better
      if save_on_best and scores[metric] > getattr(hparams, "best_" + metric):
        setattr(hparams, "best_" + metric, scores[metric])
        model.saver.save(
            sess, os.path.join(getattr(hparams, "best_" + metric + "_dir"),
                               "translate.ckpt"),
            global_step=model.global_step)
    utils.save_hparams(out_dir, hparams)
  return scores
