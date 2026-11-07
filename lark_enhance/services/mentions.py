"""Incremental, conservative @name conversion for CardKit markdown."""

from __future__ import annotations

import html
import re


class StreamMentions:
    _LEFT_BLOCKED = re.compile(r"[A-Za-z0-9_./+@#%?=&:-]")

    def __init__(self, members: dict[str, str]):
        self.members = members
        self.names = sorted(members, key=len, reverse=True)
        self.pending = ""
        self.previous = ""
        self.code_delimiter = 0

    @staticmethod
    def tag(open_id: str) -> str:
        return f'<at id="{html.escape(str(open_id), quote=True)}"></at>'

    def feed(self, text: str, final: bool = False) -> str:
        if not self.names:
            return text
        self.pending += text
        output = []
        index = 0
        while index < len(self.pending):
            char = self.pending[index]
            if char == chr(96):
                end = index + 1
                while end < len(self.pending) and self.pending[end] == chr(96):
                    end += 1
                if end == len(self.pending) and not final:
                    break  # a backtick delimiter may be split across chunks
                count = end - index
                if not self.code_delimiter:
                    self.code_delimiter = count
                elif count == self.code_delimiter or (
                    self.code_delimiter >= 3 and count >= self.code_delimiter
                ):
                    self.code_delimiter = 0
                output.append(self.pending[index:end])
                self.previous = chr(96)
                index = end
                continue
            if (
                char == "@"
                and not self.code_delimiter
                and not self._LEFT_BLOCKED.fullmatch(self.previous)
            ):
                remainder = self.pending[index + 1 :]
                if not final and any(name.startswith(remainder) for name in self.names):
                    break  # wait for the rest of the name and its right boundary
                matched = None
                for name in self.names:
                    if remainder.startswith(name):
                        suffix = remainder[len(name) :]
                        if not suffix or not re.match(r"\w", suffix[0]):
                            matched = name
                            break
                if matched is not None:
                    output.append(self.tag(self.members[matched]))
                    index += len(matched) + 1
                    self.previous = matched[-1]
                    continue
            output.append(char)
            self.previous = char
            index += 1
        self.pending = self.pending[index:]
        return "".join(output)
