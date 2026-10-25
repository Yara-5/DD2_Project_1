# Instructions for the AI assistant

Write this file before any code, and commit it first (overview, section 4.1).

## The project
<one paragraph: the option, what the tool does, the languages and tools it uses>

## How this project is checked
<the commands that decide pass or fail, for example make check and make test, and what each one runs>

## Rules the assistant follows (overview, section 4.3)
1. Pass or fail comes from the check scripts, never from the assistant's opinion.
2. Every number in a report comes from a file the flow wrote. Design rule values come from the PDK, never from the assistant.
3. Every check has a broken case in tests/broken/ that it must fail on.
4. The assistant never changes the check scripts, the broken cases or the PDK.
5. <your own rules>

## What the assistant must never do
<for example: weaken a check to make it pass, move a tag, commit files over 50 MB>

## Conventions
<code style, where files go, how commits are written, including the AI-assisted trailer>
