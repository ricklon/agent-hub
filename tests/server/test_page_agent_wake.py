"""Tests for wake-word gating of page-agent voice utterances."""

from __future__ import annotations

import pytest

from agent_hub.server.page_agent import classify_utterance

WAKE = "computer"


class TestBareWakeWord:
    """A bare wake word must be answered, not swallowed.

    It was dropped by the one-word noise filter before the wake-word check
    ran, so the "yes?" prompt was unreachable and the first thing a user
    naturally tries did nothing.
    """

    @pytest.mark.parametrize("said", ["computer", "Computer", "computer.", "Computer?"])
    def test_bare_wake_word_prompts(self, said):
        assert classify_utterance(said, WAKE) == ("command", "yes?")


class TestCommands:
    def test_wake_word_plus_command_strips_the_wake_word(self):
        kind, text = classify_utterance("computer what time is it", WAKE)
        assert (kind, text) == ("command", "what time is it")

    def test_punctuation_after_wake_word_is_stripped(self):
        kind, text = classify_utterance("Computer, what do you see?", WAKE)
        assert (kind, text) == ("command", "what do you see?")

    def test_wake_word_mid_sentence_still_triggers(self):
        kind, text = classify_utterance("hey computer turn the light on", WAKE)
        assert (kind, text) == ("command", "turn the light on")


class TestIgnored:
    def test_single_word_without_wake_word_is_noise(self):
        assert classify_utterance("hello", WAKE) == ("ignore", "hello")

    def test_empty_transcript_is_ignored(self):
        assert classify_utterance("   ", WAKE) == ("ignore", "")

    def test_speech_without_the_wake_word_is_reported_not_run(self):
        kind, text = classify_utterance("what a nice day", WAKE)
        assert (kind, text) == ("transcript", "what a nice day")


class TestNoWakeWordConfigured:
    def test_utterances_are_reported_but_never_run(self):
        """With gating disabled nothing is treated as a command.

        This is the current behaviour, not necessarily the desired one — an
        empty wake word arguably ought to mean "every utterance is for me".
        """
        kind, text = classify_utterance("what a nice day", "")
        assert (kind, text) == ("transcript", "what a nice day")

    def test_single_word_is_still_treated_as_noise(self):
        assert classify_utterance("hello", "") == ("ignore", "hello")
