import argparse
import os

import numpy as np
import torch
from datasets import load_dataset, concatenate_datasets
from transformers import (
    DataCollatorForSeq2Seq,
    set_seed, EarlyStoppingCallback, T5ForConditionalGeneration, T5Tokenizer,
)
from transformers import Seq2SeqTrainer, Seq2SeqTrainingArguments

import gadgets
import wandb

"""Parse the arguments."""
parser = argparse.ArgumentParser()
# add model id and dataset path argument
parser.add_argument("--model_name", type=str, default="google/flan-t5-xxl", help="Model id to use for training.")

parser.add_argument("--finetune_whole_model", type=bool, default=False)
parser.add_argument("--finetune_percent", type=int, default=10, choices=[10, 20, 30, 40])

# original Deepspeed arguments
# add training hyperparameters for epochs, batch size, learning rate, and seed
parser.add_argument("--epochs", type=int, default=3, help="Number of epochs to train for.")
parser.add_argument("--per_device_train_batch_size", type=int, default=8, help="Batch size to use for training.")
parser.add_argument("--per_device_eval_batch_size", type=int, default=8, help="Batch size to use for testing.")
parser.add_argument("--generation_max_length", type=int, default=140, help="Maximum length to use for generation")
parser.add_argument("--lr", type=float, default=5e-5, help="Learning rate to use for training.")
parser.add_argument("--seed", type=int, default=42, help="Seed to use for training.")
parser.add_argument("--deepspeed_config_json", type=str, default=None, help="Path to deepspeed config file.")
parser.add_argument("--gradient_checkpointing", type=bool, default=True, help="Path to deepspeed config file.")
parser.add_argument("--bf16", type=bool, default=True if torch.cuda.get_device_capability()[0] == 8 else False)

args = parser.parse_args()

# set seed
set_seed(42)

# PART: model init
gadget_model_cls = gadgets.model.gadget_assisted_model(T5ForConditionalGeneration)

model = gadget_model_cls.from_pretrained(args.model_name,
                                         # this is needed for gradient checkpointing:
                                         use_cache=False if args.gradient_checkpointing else True)
tokenizer = T5Tokenizer.from_pretrained(args.model_name)

# PART: update model for unknown tokens
gadgets.utils.add_new_token(
    "<",
    is_special=False,
    tokenizer=tokenizer,
    model=model,
    init_with=["[", ">"],
)

text = "<gadget>2+2</gadget>"
encoded = tokenizer(text, return_tensors="pt").input_ids
decoded = tokenizer.batch_decode(encoded, skip_special_tokens=True, spaces_between_special_tokens=False)
assert decoded[0] == text, decoded[0]

# PART: update generate() to support gadgets
model.prepare_for_generate(
    tokenizer,
    enabled_gadgets=[gadgets.gadget.Calculator()],
    default_max_tokens=512,
)
data_collator = DataCollatorForSeq2Seq(tokenizer, model=model)

preprocess = gadgets.prep.Preprocessing(tokenizer=tokenizer)


# PART: datasets curation
def parse_and_preprocess_aqua(example: dict[str, str]):
    example_with_gadgets = gadgets.aqua.parse(example)
    input_sample = preprocess(example_with_gadgets)
    return input_sample


def parse_and_preprocess_gsm(example: dict[str, str]):
    example = gadgets.gsm8k.parse(example)
    example = preprocess(example)
    return example


aqua = load_dataset("aqua_rat", split="train").map(parse_and_preprocess_aqua)
aqua_val = load_dataset("aqua_rat", split="validation").map(parse_and_preprocess_aqua).select(range(100))

gsm8k = load_dataset("gsm8k", "main").map(parse_and_preprocess_gsm)

train_valid_ds = gsm8k["train"].train_test_split(test_size=100, seed=42)

# upscaling GSM to the size of AQuA - we don't do that now
# gsm_idx = list(range(len(train_valid_ds["train"])))
# train_valid_ds["train"] = train_valid_ds["train"].select(random.choice(gsm_idx) for _ in range(len(aqua)))

train_ds = concatenate_datasets([train_valid_ds["train"], aqua])
train_ds = train_ds.shuffle()

valid_ds = train_valid_ds["test"]
tests_ds = gsm8k["test"]

# PART: shuffling of the logged predictions
random_rng = np.random.default_rng(42)
log_predictions_indices = random_rng.choice(
    range(len(valid_ds)),
    size=min(64, min(len(valid_ds), len(aqua_val))),
    replace=False,
)

# PART: custom evaluations' logging
metrics = gadgets.metrics.MyMetrics(
    tokenizer=tokenizer,
    log_predictions=True,
    log_predictions_indices=log_predictions_indices,
    datasets_id_length={"gsm": len(valid_ds), "aqua": len(aqua_val)}  # TODO: ordering and sizes must match eval_dataset
)

# PART: partial model finetuning
if not args.finetune_whole_model:
    finetuning_schemes = {
        10: ("lm_head.weight",
             "SelfAttention.v.weight"),
        20: ("lm_head.weight",
             "SelfAttention.v.weight",
             "SelfAttention.k.weight",
             "EncDecAttention.v.weight"),
        30: ("lm_head.weight",
             "SelfAttention.v.weight",
             "SelfAttention.k.weight",
             "SelfAttention.q.weight",
             "EncDecAttention.v.weight",
             "EncDecAttention.k.weight"),
        40: ("lm_head.weight",
             "SelfAttention.v.weight",
             "SelfAttention.k.weight",
             "SelfAttention.q.weight",
             "SelfAttention.o.weight",
             "EncDecAttention.v.weight",
             "EncDecAttention.k.weight",
             "EncDecAttention.q.weight"),
    }

    for param in model.parameters():
        param.requires_grad_(False)

    for name, param in model.named_parameters():
        if any(train_name in name for train_name in finetuning_schemes[args.finetune_percent]):
            param.requires_grad_(True)

    num_params_total = sum(p.numel() for p in model.parameters())
    num_params_trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    trainable_percent = num_params_trainable / num_params_total * 100
    print(f"Trainable parameters: {num_params_trainable} ({trainable_percent:.2f}%)")
    assert abs(trainable_percent - args.finetune_percent) < 3, \
        "Finetuning scheme is not correct. Maximum allowed difference is 3%."

wandb.init(
    project="gadgets",
    tags=[args.model_name] + args.wandb_tags.split(","),
    group=args.wandb_tags.replace(",", "-"),
    dir=args.output_dir,
)

# Define training args
# output_dir = args.repository_id if args.repository_id else args.model_id.split("/")[-1]
training_args = Seq2SeqTrainingArguments(
    output_dir=os.path.join(args.output_dir, wandb.run.name),
    per_device_train_batch_size=args.per_device_train_batch_size,
    per_device_eval_batch_size=args.per_device_eval_batch_size,
    predict_with_generate=True,
    generation_max_length=args.generation_max_length,
    fp16=False,  # T5 overflows with fp16
    bf16=args.bf16,  # Use BF16 if available
    learning_rate=args.lr,
    num_train_epochs=args.epochs,
    deepspeed=args.deepspeed_config_json,
    gradient_checkpointing=args.gradient_checkpointing,
    # logging & evaluation strategies
    logging_dir=f"{args.output_dir}/logs",
    logging_strategy="steps",
    logging_steps=500,
    evaluation_strategy="epoch",
    save_strategy="epoch",
    save_total_limit=2,
    load_best_model_at_end=True,
    # push to hub parameters
    report_to="wandb",
    include_inputs_for_metrics=True,
    metric_for_best_model="aqua_correct_results",
    greater_is_better=True,

)

# Create Trainer instance
trainer = Seq2SeqTrainer(
    model=model,
    args=training_args,
    train_dataset=train_ds,
    eval_dataset=concatenate_datasets([valid_ds, aqua_val]),
    tokenizer=tokenizer,
    data_collator=data_collator,
    compute_metrics=metrics,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=15)]
)

# Start training
trainer.train()
trainer.evaluate(eval_dataset=tests_ds, metric_key_prefix="test")

# Save our tokenizer and create model card
tokenizer.save_pretrained(args.output_dir)
