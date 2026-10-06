---
id: recipes
title: Serve a model
screen: recipes
order: 140
summary: Deploy models with dgx-serve, on a Spark, an NVIDIA box, Windows or a Mac
---
dgx-serve runs models on your machines: a DGX Spark (one, or several in tensor parallel), a Linux box with an NVIDIA GPU, a Windows PC through WSL2, or an Apple Silicon Mac.

On the machine that will serve:

```
rack init
rack up <recipe>
rack monitor up
```

`rack recipes` lists the models it knows and which machines each fits. When a model is up, add its address on [Gateways](#gateways), and the monitor's on [Cluster](#cluster).

> **Note:** a guided **Add a rack** (pairing over SSH, nothing to copy) replaces this screen in a coming version.
