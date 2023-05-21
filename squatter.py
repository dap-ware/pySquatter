#!/usr/bin/env python3
import logging
import multiprocessing
import certstream
import argparse
import re
import time
import sys
import os
import string
import requests
import json
from multiprocessing import Process, Manager, Queue
from termcolor import colored

default_padding = 60
manager = Manager()
max_domain_length = manager.Value("i", default_padding)
matched_domains = manager.dict()
queue = Queue(maxsize=10000)  # Prevent unlimited growth of the Queue


def remove_ansi_escape_codes(text):
    """
    Remove ANSI escape codes from a string.
    """
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def send_to_discord(message, webhook_url):
    """
    Send a message to a Discord channel via a webhook.
    """
    message = remove_ansi_escape_codes(message)
    data = {"content": message}
    response = requests.post(
        webhook_url, data=json.dumps(data), headers={"Content-Type": "application/json"}
    )

    if response.status_code != 204:
        raise ValueError(
            f"Request to Discord returned an error {response.status_code}, the response is:\n{response.text}"
        )


def send_to_slack(message, webhook_url):
    """
    Send a message to a Slack channel via a webhook.
    """
    message = remove_ansi_escape_codes(message)
    data = {"text": message}
    response = requests.post(
        webhook_url, data=json.dumps(data), headers={"Content-Type": "application/json"}
    )

    if response.status_code != 200:
        raise ValueError(
            f"Request to Slack returned an error {response.status_code}, the response is:\n{response.text}"
        )


class CertStreamMonitor:
    def __init__(self, patterns):
        self.patterns = [re.compile(pattern.lower()) for pattern in patterns]

    def callback(self, message, context):
        if message["message_type"] == "heartbeat":
            return

        if message["message_type"] == "certificate_update":
            all_domains = message["data"]["leaf_cert"]["all_domains"]

            for domain in all_domains:
                max_domain_length.value = max(len(domain), default_padding)
                domain_segments = domain.split(".")
                for pattern in self.patterns:
                    if any(
                        pattern.match(segment.lower()) for segment in domain_segments
                    ):
                        if (
                            domain not in matched_domains
                            or pattern.pattern not in matched_domains[domain]
                        ):
                            matched_domains.setdefault(domain, manager.list()).append(
                                pattern.pattern
                            )
                            line = f"{colored(domain, 'green'):<{max_domain_length.value + 5}} Match -> {colored(pattern.pattern, 'red')}"
                            queue.put(line)
                max_domain_length.value = default_padding

    def listen_to_certstream(self):
        while True:
            try:
                certstream.listen_for_events(
                    self.callback, url="wss://certstream.calidog.io/"
                )
            except Exception as e:
                logging.error(f"Error listening to certstream: {e}")
                time.sleep(5)  # Wait before retrying


def write_lines_to_file(output_file, discord_webhook=None, slack_webhook=None):
    with open(output_file, "w") as f:
        while True:
            line = queue.get()  # Blocks until a new line is available
            print(line)  # Print line to console
            f.write(line + "\n")
            f.flush()

            if discord_webhook:
                send_to_discord(line, discord_webhook)
            if slack_webhook:
                send_to_slack(line, slack_webhook)


def mutate_word(word):
    replacements = {
        "a": ["4", "@"],
        "e": ["3"],
        "i": ["1", "l"],
        "o": ["0"],
        "s": ["5", "$"],
        "t": ["7"],
        "b": ["8"],
        "l": ["1", "I"],
        "g": ["9"],
        "m": ["n", "nn", "rn"],
        "n": ["m", "nn", "ri"],
        "d": ["cl"],
        "u": ["v"],
        "r": ["p"],
        "c": ["e"],
    }

    combo_words = [
        "sale",
        "buy",
        "shop",
        "online",
        "official",
        "store",
        "airdrop",
        "mint",
    ]

    mutations = set([word])

    for char, replacements in replacements.items():
        for old_word in list(mutations):
            if char in old_word:
                for replacement in replacements:
                    new_word = old_word.replace(char, replacement)
                    mutations.add(new_word)

    # Typos - Insert, delete, replace, swap
    for i in range(len(word)):
        mutations.add(word[:i] + word[i + 1 :])  # delete
        for char in string.ascii_lowercase:
            mutations.add(word[:i] + char + word[i + 1 :])  # replace
            mutations.add(word[:i] + char + word[i:])  # insert
        if i < len(word) - 1:
            mutations.add(word[:i] + word[i + 1] + word[i] + word[i + 2 :])  # swap

    for combo_word in combo_words:
        mutations.update([word + combo_word, combo_word + word])

    # Writing mutations to a file
    with open(f"{word}_mutations.txt", "w") as f:
        for mutation in mutations:
            f.write(f"{mutation}\n")

    return list(mutations)


def print_banner():
    separator_length = 70  # Adjust the width of the separator as desired
    separator = "─" * separator_length
    banner_text = """
        ┌─┐┌─┐ ┬ ┬┌─┐┌┬┐┌┬┐┌─┐┬─┐
        └─┐│─┼┐│ │├─┤ │  │ ├┤ ├┬┘
        └─┘└─┘└└─┘┴ ┴ ┴  ┴ └─┘┴└─
    """
    banner_lines = banner_text.strip().split("\n")
    padding = (separator_length - len(banner_lines[0].strip())) // 2

    print(f"\033[1;31m{separator}\033[0m")
    for line in banner_lines:
        print(f"\033[1;32m{' ' * padding}{line.strip()}\033[0m")
    print("                               dap?")
    print(f"\033[1;31m{separator}\033[0m")


def main():
    print_banner()
    parser = argparse.ArgumentParser(
        description="Monitor Certstream for specific patterns."
    )
    parser.add_argument("-f", "--file", type=str, help="File with patterns to monitor.")
    parser.add_argument(
        "-o",
        "--output",
        default="matches.txt",
        type=str,
        help="File to write matched patterns to.",
    )
    parser.add_argument(
        "-m",
        "--mutate",
        nargs="+",
        type=str,
        help="Generate and use mutations of these words instead of a patterns file.",
    )
    parser.add_argument("--discord-webhook", type=str, help="Discord webhook URL.")
    parser.add_argument("--slack-webhook", type=str, help="Slack webhook URL.")
    args = parser.parse_args()

    # Check if the input file exists and is readable
    if args.file and not os.path.isfile(args.file):
        raise Exception(f"File {args.file} does not exist or is not readable.")

    # Check if the output file is writable
    try:
        with open(args.output, "w") as f:
            pass
    except IOError:
        raise Exception(f"Cannot write to file {args.output}")

    # Validate the input word(s) for mutation
    if args.mutate:
        for word in args.mutate:
            if not re.match("^[a-zA-Z0-9-]*$", word):
                raise Exception(f"Invalid characters in word: {word}")

    if args.mutate:
        patterns = []
        for word in args.mutate:
            patterns += mutate_word(word)
    elif args.file:
        with open(args.file) as f:
            patterns = [line.strip() for line in f]
    else:
        raise Exception("No input patterns or word to mutate provided.")

    monitor = CertStreamMonitor(patterns)

    listener_process = multiprocessing.Process(target=monitor.listen_to_certstream)
    listener_process.start()

    write_process = multiprocessing.Process(
        target=write_lines_to_file,
        args=(args.output, args.discord_webhook, args.slack_webhook),
    )
    write_process.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        listener_process.terminate()
    finally:
        listener_process.join()
        write_process.join()
        print("\nExiting...")


if __name__ == "__main__":
    main()
