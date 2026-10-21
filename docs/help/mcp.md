---
id: mcp
title: Tools (MCP)
screen: mcp
order: 90
summary: Let the model read files, search, fetch pages or run commands
---
Tools come from MCP servers: small programs ByteBunker starts on this machine. Add one from the catalog on [MCP](#mcp), or by its command. Each server's tools appear in the Playground when tools are on.

A folder a server is given, such as the Filesystem server's root or the terminal's working directory, is created when it is not there yet. A Git server needs a repository that already exists. When a server cannot start, its row says why, in the server's own words.

## Before a tool runs

In [bb](help:cli), and in the Playground when chats run on the server ([Settings](#settings), **Chats**), what a tool says about itself decides whether it asks: a tool that only reads runs; anything that may change something asks first, with a card here or a prompt in the terminal. **Always allow** stops the asking for that tool. Change it per tool with [profile](help:profiles) rules.

## The terminal tool

The built-in terminal gives the model a shell on this machine (zsh or bash, PowerShell on Windows), as you, with your files and keys. It is off until you turn it on: **enable** on its row, or **Turn on** on its catalog card, which also lets you pick the folder it starts in. There is one terminal; its card changes that one rather than adding another. Where tools ask, its commands ask first.

> **Warning:** a shell is a shell. Turn it on when you want the model to have one, and off when you do not.

## Burp Suite

The **Burp Suite** catalog entry connects to Burp over its MCP Server extension, so the model can send HTTP/1.1 and HTTP/2 requests, open Repeater and Intruder tabs, read proxy history, list scanner issues and mint Collaborator payloads — for authorised testing only. First, inside Burp: install **MCP Server** from Extensions → BApp Store and enable it under Settings → MCP (it serves on `http://127.0.0.1:9876/` by default). Then add it here; ByteBunker speaks to it through a small stdio bridge (`mcp-remote`). Burp must be running for the tools to appear.

To give the tools to [agents](help:agents) instead of only chat, share the `burp` server under [Access](help:court) and add the tools you want to a role (for example `burp__send_http1_request`). Agents reach it through the host, so a minion's sandbox stays without network.
