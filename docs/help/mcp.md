---
id: mcp
title: Tools (MCP)
screen: mcp
order: 90
summary: Let the model read files, search, fetch pages or run commands
---
Tools come from MCP servers: small programs ByteBunker starts on this machine. Add one from the catalog on [MCP](#mcp), or by its command. Each server's tools appear in the Playground when tools are on.

## Before a tool runs

In [bb](help:cli), and in the Playground when chats run on the server ([Settings](#settings), **Chats**), what a tool says about itself decides whether it asks: a tool that only reads runs; anything that may change something asks first, with a card here or a prompt in the terminal. **Always allow** stops the asking for that tool. Change it per tool with [profile](help:profiles) rules.

## The terminal tool

The built-in terminal gives the model a shell on this machine (zsh or bash, PowerShell on Windows), as you, with your files and keys. It is off until you turn it on, and where tools ask, its commands ask first.

> **Warning:** a shell is a shell. Turn it on when you want the model to have one, and off when you do not.
