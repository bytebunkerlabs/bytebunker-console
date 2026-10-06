---
id: cluster
title: Rack monitors
screen: cluster
order: 110
summary: Every node, GPU and engine, from one monitor per rack
---
The Cluster screen shows every machine a rack monitor reports: GPUs, memory, temperatures, and each engine's tokens per second and queue.

## Add a monitor

On the rack's head node, with dgx-serve:

```
rack monitor up
```

It prints an address with its token, like `http://rack:TOKEN@192.0.2.10:9177`. Paste that on [Cluster](#cluster), **Monitors**, **Add**. The token stays on this machine.

To see it again later:

```
rack monitor token
```

> **Note:** the monitor only reads. Starting and stopping models happens through dgx-serve, never through the monitor.
