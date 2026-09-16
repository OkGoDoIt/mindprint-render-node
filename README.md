# Mindprint render node — setup notes for the machine's owner

This is the NixOS module for running a Mindprint render node on a desktop with an NVIDIA card.
The node asks Mindprint for image-generation work over outbound HTTPS, renders it on the card,
uploads the image, and asks again. Mindprint never connects in; the machine never listens.

Everything below is what the module does, what it needs from you, and how to check it. It is
written so that the three machines can carry an identical configuration and any of them can be
switched on by setting one option.

## Quick start (per machine)

1. **Mint the machine's credential.** Signed in to Mindprint as an operator, open
   https://beta.mindprint.ai/admin/render-nodes/enroll, enter the machine's name (its hostname
   is fine) and press the button. The page shows the credential once, with a **Copy** button
   and a **Download** button that saves it as a one-line `mindprint-render-token` file, plus
   the same steps as below with the values filled in. One credential per machine.
2. **Add the module** to the machine's flake and enable it (the snippet under *Install*):
   the flake input, `imports = [ inputs.mindprint-render.nixosModules.default ]`, and
   `services.mindprint-render = { enable = true; tokenFile = "…"; gpuIndex = 0; }`.
3. **Place the credential** where `tokenFile` points, readable by the `mindprint-render` user
   — a sops-nix / agenix secret owned by it, or after the rebuild:
   `sudo install -m 0600 -o mindprint-render -g mindprint-render /dev/stdin /var/lib/mindprint-render/state/token <<< 'mprn_…'`
   with `tokenFile` left at its default.
4. **Rebuild.** Within five minutes the updater downloads the worker, builds its Python
   environments (5–15 minutes the first time) and self-tests; the machine then appears on
   Mindprint's Render nodes page, where the model it holds is chosen. To check on the
   machine: `mindprint-render-node status` and `mindprint-render-node selftest`.

The rest of this file is the detail behind those four steps.

## What it does on the machine

- Creates a system user `mindprint-render` (no login, no sudo, no home outside its state dir).
- Creates `/var/lib/mindprint-render/{state,releases,models,cache,venvs}` (see *Disk* below).
- Installs two systemd units: `mindprint-render-node.service` (the agent, `Restart=always`,
  hardened: `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`, the state
  dir the only writable path, the NVIDIA device nodes the only devices, `Nice=10`) and
  `mindprint-render-updater.timer` (a oneshot every five minutes that pulls the release
  Mindprint advertises, verifies its SHA-256, builds the Python environments, self-tests, and
  flips a `current` symlink — with automatic rollback if the new one does not come up).
- Enables `programs.nix-ld` and adds four libraries to its list (`stdenv.cc.cc.lib`, `zlib`,
  `glib`, `libGL`). The model runtimes are the same pip wheels the production predictors pin
  (PyTorch's CUDA builds), which are manylinux binaries; nix-ld is how they run on NixOS. If you
  already manage nix-ld, the lists merge. This is the module's only system-wide effect.
- Puts one command on the PATH: `mindprint-render-node` (`status`, `pause`, `resume`,
  `selftest`).

It does **not** open a port, change the firewall, touch Tailscale or any network setting, add a
sudo rule, install Docker, or run anything as root after the rebuild. Its only listening socket
is a Unix socket inside its own state directory. Outbound connections go to
`beta.mindprint.ai:443`, and — for downloads only — `huggingface.co` (+ its CDN hosts) for model
weights and `pypi.org` / `files.pythonhosted.org` / `download.pytorch.org` for wheels.

## What it needs

- NixOS with the NVIDIA driver working (`nvidia-smi` shows the card). The module reads
  `hardware.nvidia.package` for `nvidia-smi` and uses `/run/opengl-driver/lib` for `libcuda`;
  it changes neither.
- Python 3.11 from nixpkgs (`pkgs.python311`, overridable) and `uv`, both pulled by the module.
- **Disk** under `/var/lib/mindprint-render`: the two Python environments are about 12 GB
  together; weights are downloaded once per model and kept — FLUX.2-klein ≈ 16 GB, Z-Image
  (Turbo or Base) ≈ 20 GB each, SDXL ≈ 7 GB, SD 1.5 ≈ 3 GB. Budget 30 GB for one Z-Image lane,
  100 GB if every lane ends up on the machine. If `/var/lib` is small, set `stateDir` to a path
  on the big disk.
- One credential per machine (a line like `mprn_rn-xxxxxxxxxxxx_<64 hex>`). You mint it
  yourself: signed in to Mindprint as an operator, open
  https://beta.mindprint.ai/admin/render-nodes/enroll, name the machine, and the page shows
  the credential once, with a download button for the token file and the exact install
  command. It authorizes only the render-node API at Mindprint and can be revoked from our
  side at any time. Do not reuse one credential on two machines: to us that looks like one node
  flapping between two cards, and both will keep interrupting each other.

## Install

Add the flake input and the module, then enable it on the hosts you choose:

```nix
{
  inputs.mindprint-render.url = "github:OkGoDoIt/mindprint-render-node";
  # No inputs of its own — it takes pkgs from your system, so no nixpkgs pin and no lock churn.

  # In the host's configuration:
  imports = [ inputs.mindprint-render.nixosModules.default ];

  services.mindprint-render = {
    enable = true;
    tokenFile = "/run/secrets/mindprint-render-token";   # or wherever your secrets land
    gpuIndex = 0;                                         # which CUDA device this node owns
  };
}
```

Options with their defaults, all optional:

| option | default | what it is |
|---|---|---|
| `server` | `https://beta.mindprint.ai` | the Mindprint host it talks to |
| `gpuIndex` | `0` | the CUDA device; one node = one card |
| `tokenFile` | `<stateDir>/state/token` | the credential, one line, readable by `mindprint-render` (0600 owned by it, or 0640 in its group); a sops-nix / agenix path works |
| `stateDir` | `/var/lib/mindprint-render` | releases, weights, environments, state |
| `updateEvery` | `5min` | the updater's cadence |
| `python` | `pkgs.python311` | interpreter for the agent and the environments |
| `extraLibraries` | `[]` | more libraries for nix-ld, if a wheel wants one we did not list |
| `extraPackages` | `[]` | more on the services' PATH |
| `extraEnvironment` | `{}` | e.g. `HTTPS_PROXY` |

If you would rather not depend on a GitHub flake input, the module is two files
(`mindprint-render.nix`, `updater.py`); vendoring them works the same, and they change rarely —
the worker itself and everything that runs the models arrive through the updater, not through
this module.

## The credential

Mint one per machine at https://beta.mindprint.ai/admin/render-nodes/enroll (you need to be
signed in as an operator). Without a token file the services start, log "no credential", and
do nothing. With one, the
next updater run (at most five minutes; `systemctl start mindprint-render-updater.service`
runs it now) downloads the release, builds the two Python environments (5–15 minutes the first
time, ~6 GB of wheels), self-tests, and starts the agent. The agent then shows up on our side.

Which model a card holds is chosen from Mindprint, not on the machine: once the node appears,
Roger assigns it a model and the node downloads those weights (once) and loads them. Any of the
three machines can hold any model; nothing per-model is configured here.

## Check that it works

```bash
systemctl status mindprint-render-updater.timer mindprint-render-node.service
journalctl -u mindprint-render-updater -n 100 --no-pager    # download, environments, self-test
journalctl -u mindprint-render-node -n 50 --no-pager        # heartbeats, model load, renders
mindprint-render-node status                                # what it holds, what it is doing
mindprint-render-node selftest                              # imports, torch sees the card, weights
```

The self-test prints one line per runtime with the torch/CUDA versions and the card name, and
ends with `self-test passed` — or `FAIL: …` lines. If anything fails, the output of the two
`journalctl` commands and of `selftest` is all we need to fix it from our side (the most likely
first-time failure is a wheel wanting a system library, which is one entry in `extraLibraries`
or, better, a fix we ship).

## Module changes (bump the flake input)

The module rarely changes, and when it does you will hear from us. Changes so far:

- **2026-09-16** — `LD_LIBRARY_PATH` now carries the nix-ld library list beside the driver path
  (torch's `dlopen` from the Nix python never consults `NIX_LD_LIBRARY_PATH`; the first install on
  pheonix failed its self-test on `libstdc++.so.6` for exactly this). The `extraEnvironment`
  workaround is no longer needed. The updater unit and the `mindprint-render-node` hand tool
  now run under the agent's sandbox (`ProtectSystem=strict`, `ProtectHome`, `ProtectKernel*`,
  `NoNewPrivileges`, `RestrictNamespaces`, the GPU nodes the only devices, the state dir the only
  writable path).

Everything else — the agent, the model runtimes, the updater's own logic — arrives through the
updater and needs nothing from you. The updater refuses an advert whose URL is not this node's
own server under the release route, follows no redirects, and refuses a version or file name
that is not a plain name. A release it refused (a failed self-test, say, because of a host
problem since fixed) is tried again after six hours, up to six times; `mindprint-render-node
retry-update` forgets the refusals so the next run (within five minutes) tries at once — no root.

## Day to day

- **Using the card yourself:** nothing to do. The agent finishes the render it holds and stops
  claiming while the **total** memory held by processes other than its own exceeds the threshold
  (4 GB by default — a compositor or a browser's GPU process is under that; a game, a training
  run, or a resident Whisper daemon plus a browser may not be). The threshold is a Mindprint-side setting we can raise; tell us if
  something legitimate on the machine sits above it. It runs at `Nice=10`. When the memory is
  released it resumes.
- **Stop it:** `systemctl stop mindprint-render-node` — the card is free within one render
  (seconds for most models, under a minute for Z-Image). `start` brings it back.
- **Pause across reboots:** `mindprint-render-node pause` / `resume`.
- **Suspend / sleep:** fine. A render interrupted by sleep is re-done elsewhere; the node
  reconnects on wake.
- **Updates:** automatic, from Mindprint, verified by checksum, with a self-test before and a
  health check after (and a rollback if the health check fails). Nothing is fetched from GitHub
  after the rebuild. We will tell you if this module itself ever needs a newer commit.
- **Remove it:** `services.mindprint-render.enable = false;`, rebuild, `rm -rf
  /var/lib/mindprint-render`. The credential can be revoked from our side at any time.

## Which machine holds which model

Decided from our side, per machine, from the card and the host it reports:

- **SDXL** (~7 GB) and **SD 1.5** fit any of the cards resident.
- **FLUX.2-klein** (~16 GB) runs resident on a 24 GB card.
- **Z-Image** (Turbo or Base, ~20.5 GB of bf16 weights) does not reliably fit beside a desktop
  compositor on a 24 GB card, so there the node starts it with per-component CPU offload — each
  stage moves to the card for its turn, a few seconds per image over PCIe — which keeps the
  weights in system RAM and needs **~24 GB of free RAM**. A host with less (pheonix: 32 GB
  installed, ~23 GB idle) refuses the lane with a sentence on our page rather than swapping the
  desktop; it is a lane for the boxes with more RAM. Nothing to configure on the machine.
