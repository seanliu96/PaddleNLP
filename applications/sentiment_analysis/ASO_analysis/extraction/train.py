# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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


import argparse
import os
import random
import warnings
from functools import partial

import numpy as np
import paddle
import paddle.nn.functional as F
from data import convert_example_to_feature, load_dict
from datasets import load_dataset
from evaluate import evaluate

from paddlenlp.data import DataCollatorForTokenClassification
from paddlenlp.metrics import ChunkEvaluator
from paddlenlp.transformers import (
    LinearDecayWithWarmup,
    SkepForTokenClassification,
    SkepTokenizer,
)

warnings.filterwarnings("ignore")


def set_seed(seed):
    paddle.seed(seed)
    random.seed(seed)
    np.random.seed(seed)


def train():
    # set running envir
    model_name = "skep_ernie_1.0_large_ch"

    paddle.set_device(args.device)
    set_seed(args.seed)

    if not os.path.exists(args.checkpoints):
        os.mkdir(args.checkpoints)

    # load and process data
    label2id, id2label = load_dict(args.label_path)
    datasets = load_dataset("text", data_files={"dev": args.dev_path, "train": args.train_path})

    tokenizer = SkepTokenizer.from_pretrained(model_name)
    trans_func = partial(
        convert_example_to_feature, tokenizer=tokenizer, label2id=label2id, max_seq_len=args.max_seq_len
    )
    train_ds = datasets["train"].map(trans_func, batched=False, remove_columns=["text"])
    dev_ds = datasets["dev"].map(trans_func, batched=False, remove_columns=["text"])

    data_collator = DataCollatorForTokenClassification(tokenizer, label_pad_token_id=label2id["O"])

    train_batch_sampler = paddle.io.BatchSampler(train_ds, batch_size=args.batch_size, shuffle=True)
    dev_batch_sampler = paddle.io.BatchSampler(dev_ds, batch_size=args.batch_size, shuffle=False)
    train_loader = paddle.io.DataLoader(train_ds, batch_sampler=train_batch_sampler, collate_fn=data_collator)
    dev_loader = paddle.io.DataLoader(dev_ds, batch_sampler=dev_batch_sampler, collate_fn=data_collator)

    # configure model training
    model = SkepForTokenClassification.from_pretrained(model_name, num_classes=len(label2id))

    num_training_steps = len(train_loader) * args.num_epochs
    lr_scheduler = LinearDecayWithWarmup(
        learning_rate=args.learning_rate, total_steps=num_training_steps, warmup=args.warmup_proportion
    )
    decay_params = [p.name for n, p in model.named_parameters() if not any(nd in n for nd in ["bias", "norm"])]
    grad_clip = paddle.nn.ClipGradByGlobalNorm(args.max_grad_norm)
    optimizer = paddle.optimizer.AdamW(
        learning_rate=lr_scheduler,
        parameters=model.parameters(),
        weight_decay=args.weight_decay,
        apply_decay_param_fun=lambda x: x in decay_params,
        grad_clip=grad_clip,
    )

    metric = ChunkEvaluator(label2id.keys())

    # start to train model
    global_step, best_f1 = 1, 0.0
    model.train()
    for epoch in range(1, args.num_epochs + 1):
        for batch_data in train_loader():
            input_ids, token_type_ids, labels = (
                batch_data["input_ids"],
                batch_data["token_type_ids"],
                batch_data["labels"],
            )
            logits = model(input_ids, token_type_ids=token_type_ids)
            loss = F.cross_entropy(logits.reshape([-1, len(label2id)]), labels.reshape([-1]), ignore_index=-1)

            loss.backward()
            lr_scheduler.step()
            optimizer.step()
            optimizer.clear_grad()

            if global_step > 0 and global_step % args.log_steps == 0:
                print(
                    f"epoch: {epoch} - global_step: {global_step}/{num_training_steps} - loss:{loss.numpy().item():.6f}"
                )
            if (global_step > 0 and global_step % args.eval_steps == 0) or global_step == num_training_steps:
                precision, recall, f1 = evaluate(model, dev_loader, metric)
                model.train()
                if f1 > best_f1:
                    print(f"best F1 performence has been updated: {best_f1:.5f} --> {f1:.5f}")
                    best_f1 = f1
                    paddle.save(model.state_dict(), f"{args.checkpoints}/best.pdparams")
                print(f"evaluation result: precision: {precision:.5f}, recall: {recall:.5f},  F1: {f1:.5f}")

            global_step += 1

    paddle.save(model.state_dict(), f"{args.checkpoints}/final.pdparams")


if __name__ == "__main__":
    # yapf: disable
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--num_epochs", type=int, default=3, help="Number of epoches for training.")
    parser.add_argument("--train_path", type=str, default=None, help="The path of train set.")
    parser.add_argument("--dev_path", type=str, default=None, help="The path of dev set.")
    parser.add_argument("--label_path", type=str, default=None, help="The path of label dict.")
    parser.add_argument("--batch_size", type=int, default=32, help="Total examples' number in batch for training.")
    parser.add_argument("--max_seq_len", type=int, default=512, help="Batch size per GPU/CPU for training.")
    parser.add_argument("--learning_rate", type=float, default=5e-5, help="The initial learning rate for optimizer.")
    parser.add_argument("--weight_decay", type=float, default=0.01, help="Weight decay rate for L2 regularizer.")
    parser.add_argument("--max_grad_norm", type=float, default=1.0, help="Max grad norm to clip gradient.")
    parser.add_argument("--warmup_proportion", type=float, default=0.1, help="Linear warmup proportion over the training process.")
    parser.add_argument("--log_steps", type=int, default=50, help="Frequency of printing log.")
    parser.add_argument("--eval_steps", type=int, default=500, help="Frequency of performing evaluation.")
    parser.add_argument("--seed", type=int, default=1000, help="Random seed for initialization.")
    parser.add_argument('--device', choices=['cpu', 'gpu'], default="gpu", help="Select which device to train model, defaults to gpu.")
    parser.add_argument("--checkpoints", type=str, default=None, help="Directory to save checkpoint.")

    args = parser.parse_args()
    # yapf: enable

    train()
