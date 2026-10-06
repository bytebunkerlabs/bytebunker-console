---
id: models
title: Models and their windows
screen: models
order: 25
summary: What each model can do, and how ByteBunker knows
---
The Models screen lists every model every gateway serves, and through which gateway.

## The window

A model's context window is the prompt and the answer together. ByteBunker takes it, best first, from what the engine said when it refused a prompt as too long, from what the engine says it serves, and only then from its own table. A learned window is kept across restarts.

## Thinking and effort

Thinking models can be asked to think less or more. The effort setting (Default, Off, Low, Medium, High, Max) is sent as the model's own nearest level, and only to models that accept one.
