---
id: gateways
title: Connect a model server
screen: gateways
order: 20
summary: Add an engine or a gateway by address, or let ByteBunker find them
---
A gateway is any server that answers the OpenAI chat API: an engine dgx-serve runs (vLLM, llama.cpp), or a router in front of several. ByteBunker merges the models of every gateway into one list and sends each request to the gateway that serves its model.

## Find them

Press **Find engines** on the [Gateways](#gateways) screen. ByteBunker looks on this machine, on your tailnet's own devices, and, if you allow it, on your local network.

> **Note:** on a Mac, the first search asks for **Local Network** access. Allow it, or nothing on your network can be found or reached.

## Add one by address

An address and a port is enough: `192.0.2.10:8000` becomes `http://192.0.2.10:8000/v1`. If the server needs a key, paste it in the key field; it is stored in `config.json` on this machine (readable only by you).

To check a server from a terminal first:

```
curl -s http://192.0.2.10:8000/v1/models
```

A list of models means it works. `401` means it needs a key.

## Models served twice

When two gateways list the same model (an engine and a router in front of it), the first one wins the plain name and the other is listed as `model@gateway`. Pick that one to pin a request to it.

## Serving a model of your own

dgx-serve deploys models on a DGX Spark, an NVIDIA Linux box, a Windows PC with WSL2, or a Mac, and gives you the address to add here. → [Serve a model](help:recipes)
