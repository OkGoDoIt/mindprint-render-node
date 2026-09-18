# The Mindprint render node as a NixOS module (D300).
#
# Import it and set the options; nothing else on the box changes. It creates a service user, a
# state tree under /var/lib/mindprint-render, the agent service, the updater timer, and the nix-ld
# shim that lets the pip-installed CUDA wheels the production predictors pin run on NixOS. It
# opens no firewall port, touches no network configuration, and grants no sudo.
#
#   inputs.mindprint-render.url = "github:OkGoDoIt/mindprint-render-node";
#   imports = [ inputs.mindprint-render.nixosModules.default ];
#   services.mindprint-render = {
#     enable = true;
#     tokenFile = "/run/secrets/mindprint-render-token";   # one line, readable by mindprint-render
#     gpuIndex = 0;
#   };
#
# After the rebuild the updater's first run (two minutes after boot, then every `updateEvery`)
# downloads the release the Mindprint server advertises into /var/lib/mindprint-render/releases/<v>,
# builds the runtimes' Python environments, self-tests, points `current` at it, and the agent
# starts. Everything after that — worker code, model runtimes, which model this card holds — is
# driven from the Mindprint side; this module only needs a rebuild if its options change.
#
# What the owner keeps: `systemctl stop mindprint-render-node` (the card is free within one
# render), `mindprint-render-node pause` / `resume` (a pause that survives reboots), and the whole
# thing goes away with `services.mindprint-render.enable = false`. `keepRenders` (D319) keeps a
# copy of every image the node renders, with its generation written into the PNG, for the owner
# to browse; Mindprint's page shows whether it is on and prints the snippet that changes it.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.mindprint-render;
  home = cfg.stateDir;
  python = cfg.python;
  # The runtimes' pip wheels (torch cu126 / cu121) are ordinary manylinux binaries; on NixOS they
  # need the dynamic loader nix-ld provides, a handful of system libraries, and the driver's
  # libcuda from the graphics driver path. Everything else they need is inside the wheels.
  runtimeLibs = (with pkgs; [ stdenv.cc.cc.lib zlib glib libGL ]) ++ cfg.extraLibraries;
  driverLibs = "/run/opengl-driver/lib";
  environment = {
    MINDPRINT_RENDER_HOME = home;
    MINDPRINT_RENDER_STATE = "${home}/state";
    MINDPRINT_RENDER_SERVER = cfg.server;
    MINDPRINT_RENDER_GPU = toString cfg.gpuIndex;
    MINDPRINT_RENDER_TOKEN_FILE = cfg.tokenFile;
    # torch dlopen()s its shared objects from the Nix python3, which never goes through nix-ld, so
    # NIX_LD_LIBRARY_PATH is never consulted for them — only LD_LIBRARY_PATH is (Allan, 2026-09-16:
    # "libstdc++.so.6: cannot open shared object file" on every runtime with the driver path alone).
    # Both carry the same list; the driver's libcuda lives under /run/opengl-driver on NixOS.
    LD_LIBRARY_PATH = "${lib.makeLibraryPath runtimeLibs}:${driverLibs}";
    NIX_LD_LIBRARY_PATH = "${lib.makeLibraryPath runtimeLibs}:${driverLibs}";
    NIX_LD = "${pkgs.stdenv.cc.bintools.dynamicLinker}";
    PYTHONUNBUFFERED = "1";
    HOME = home;
    # The owner's local copy of every render (D319): declared here, reported by the agent on every
    # heartbeat, never switched from Mindprint's side. Absent variable = a module before the option.
    MINDPRINT_RENDER_KEEP_RENDERS = if keep.enable then "1" else "0";
    MINDPRINT_RENDER_RENDERS_DIR = keep.directory;
    MINDPRINT_RENDER_RENDERS_MAX_MB = toString keep.maxSizeMb;
    MINDPRINT_RENDER_RENDERS_GROUP = keep.group;
  } // cfg.extraEnvironment;
  keep = cfg.keepRenders;
  keepOutside = keep.enable && !(lib.hasPrefix (home + "/") keep.directory);
  nvidiaBin = lib.optional (config.hardware.nvidia.package or null != null) config.hardware.nvidia.package.bin;
  path = [ python pkgs.uv pkgs.coreutils pkgs.bash pkgs.gnutar pkgs.gzip pkgs.procps pkgs.systemd ]
    ++ nvidiaBin ++ cfg.extraPackages;
  updater = pkgs.writeText "mindprint-render-updater.py" (builtins.readFile ./updater.py);
  # The GPU device nodes are the only devices either unit may touch; the self-test the updater runs
  # needs them as much as the agent does.
  deviceAllow = [ "/dev/nvidia0 rw" "/dev/nvidia1 rw" "/dev/nvidia2 rw" "/dev/nvidia3 rw" "/dev/nvidiactl rw" "/dev/nvidia-uvm rw" "/dev/nvidia-uvm-tools rw" "/dev/nvidia-modeset rw" "/dev/dri rw" "char-nvidia-caps rw" ];
  # Hardening shared by the agent, the updater and the hand tool: the state tree is the only
  # writable place, no new privileges, no home directories, no kernel knobs.
  sandbox = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectHostname = true;
    RestrictSUIDSGID = true;
    RestrictRealtime = true;
    RestrictNamespaces = true;
    LockPersonality = true;
    SystemCallArchitectures = "native";
    ReadWritePaths = [ home ] ++ lib.optional keepOutside keep.directory;
    DeviceAllow = deviceAllow;
  };
  sandboxArgs = lib.concatStringsSep " " (
    lib.mapAttrsToList (k: v:
      if builtins.isList v then lib.concatMapStringsSep " " (x: "--property=${k}=\"${x}\"") v
      else "--property=${k}=${if builtins.isBool v then (if v then "true" else "false") else toString v}")
      sandbox);
in
{
  options.services.mindprint-render = {
    enable = lib.mkEnableOption "the Mindprint render node (pulls generation work over outbound HTTPS)";

    server = lib.mkOption {
      type = lib.types.str;
      default = "https://beta.mindprint.ai";
      description = "The Mindprint control plane the node talks to. Outbound HTTPS only.";
    };

    gpuIndex = lib.mkOption {
      type = lib.types.int;
      default = 0;
      description = "Which CUDA device this node owns (one agent = one GPU = one render at a time).";
    };

    tokenFile = lib.mkOption {
      type = lib.types.str;
      default = "${cfg.stateDir}/state/token";
      description = ''
        The node's credential, one line, readable by the `mindprint-render` user (0600 owned by it,
        or 0640 in its group). A sops-nix / agenix secret path works. The credential only ever
        authorizes the render-node API; it opens nothing else at Mindprint.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/mindprint-render";
      description = "Where releases, model weights, Python environments and the agent's state live. Budget ~80 GB.";
    };

    updateEvery = lib.mkOption {
      type = lib.types.str;
      default = "5min";
      description = "How often the updater asks the server which release it should be running (systemd time span).";
    };

    python = lib.mkOption {
      type = lib.types.package;
      default = pkgs.python311;
      defaultText = lib.literalExpression "pkgs.python311";
      description = "The interpreter for the agent and for the runtimes' venvs. The pinned wheels are built for 3.11.";
    };

    extraLibraries = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      description = "System libraries to expose to the pip wheels through nix-ld, beyond libstdc++, zlib, glib and libGL.";
    };

    extraPackages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      description = "Packages to add to the services' PATH (a different nvidia-smi, a proxy helper, …).";
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra environment for both services (HTTPS_PROXY, HF_HUB_ENABLE_HF_TRANSFER, …).";
    };

    keepRenders = {
      enable = lib.mkEnableOption "keeping a local copy of every image this node renders, for the machine's owner to browse";

      directory = lib.mkOption {
        type = lib.types.str;
        default = "${cfg.stateDir}/renders";
        defaultText = lib.literalExpression ''"''${stateDir}/renders"'';
        description = ''
          Where the copies go: `<directory>/<YYYY-MM-DD>/<prediction>.png`, one file per render, 0640,
          in a setgid `2750` tree owned by `mindprint-render:<group>`. A path outside `stateDir` is
          added to the services' writable paths.
        '';
      };

      maxSizeMb = lib.mkOption {
        type = lib.types.int;
        default = 10240;
        description = "The tree's size cap in MB; past it the oldest days go first. 0 = no cap.";
      };

      group = lib.mkOption {
        type = lib.types.str;
        default = "mindprint-render";
        description = ''
          The group that can browse the tree. With the default, add your user to `mindprint-render`
          (`users.users.<you>.extraGroups = [ "mindprint-render" ]`). Any other group needs a
          `directory` outside `stateDir`, whose root only the service group may enter.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !keep.enable || keep.group == "mindprint-render" || keepOutside;
        message = "services.mindprint-render.keepRenders: a group other than mindprint-render can only browse a directory outside stateDir (${home} is 0750 to the service group); set keepRenders.directory to a path elsewhere, e.g. /srv/mindprint-renders.";
      }
    ];

    warnings = lib.optional (!(config.hardware.graphics.enable or false) && nvidiaBin == [ ])
      "services.mindprint-render: no NVIDIA driver appears to be configured (hardware.nvidia / hardware.graphics); the node will start but its self-test will fail until one is.";

    # The pip-installed CUDA wheels are unpatched ELF binaries; nix-ld is how NixOS runs those.
    # This is the one system-wide effect of the module: the /lib64 loader stub. The libraries
    # list merges with anything the machine already sets.
    programs.nix-ld.enable = true;
    programs.nix-ld.libraries = runtimeLibs;

    users.users.mindprint-render = {
      isSystemUser = true;
      group = "mindprint-render";
      home = home;
      createHome = false;
      description = "Mindprint render node service identity (no login, no sudo)";
      # /dev/nvidia* is group video on most driver setups; render for DRM nodes.
      extraGroups = [ "video" "render" ];
    };
    users.groups.mindprint-render = { };
    users.groups.render = { };

    systemd.tmpfiles.rules = [
      "d ${home} 0750 mindprint-render mindprint-render -"
      "d ${home}/state 0750 mindprint-render mindprint-render -"
      "d ${home}/releases 0750 mindprint-render mindprint-render -"
      "d ${home}/models 0750 mindprint-render mindprint-render -"
      "d ${home}/cache 0750 mindprint-render mindprint-render -"
      "d ${home}/venvs 0750 mindprint-render mindprint-render -"
    ] ++ lib.optional keep.enable "d ${keep.directory} 2750 mindprint-render ${keep.group} -";

    # The hand tool, for whoever is at the keyboard: status, pause, resume, selftest, bench.
    environment.systemPackages = [
      (pkgs.writeShellScriptBin "mindprint-render-node" ''
        if [ ! -e "${home}/current/bin/mindprint-render-node" ]; then
          echo "mindprint-render-node: no release installed yet." >&2
          echo "The updater runs two minutes after boot and every ${cfg.updateEvery}; see:" >&2
          echo "  systemctl status mindprint-render-updater.timer" >&2
          echo "  journalctl -u mindprint-render-updater -n 50 --no-pager" >&2
          echo "  systemctl start mindprint-render-updater.service   # run it now" >&2
          exit 2
        fi
        exec ${pkgs.systemd}/bin/systemd-run --quiet --pipe --wait --collect \
          --uid=mindprint-render --gid=mindprint-render \
          --property=WorkingDirectory=${home} ${sandboxArgs} \
          ${lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "--setenv=${k}=${v}") environment)} \
          --setenv=PATH=${lib.makeBinPath path} \
          ${home}/current/bin/mindprint-render-node "$@"
      '')
    ];

    systemd.services.mindprint-render-node = {
      description = "Mindprint render node (pulls generation work; one GPU, one model at a time)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      inherit environment path;
      serviceConfig = {
        Type = "exec";
        User = "mindprint-render";
        Group = "mindprint-render";
        WorkingDirectory = home;
        # `current` is the updater's symlink; until the first update lands this fails and
        # Restart=always simply tries again — no bootstrap step to forget.
        ExecStart = "${home}/current/bin/run-agent";
        Restart = "always";
        RestartSec = 10;
        # SIGTERM: the agent stops claiming and finishes the render it holds — one render is the
        # most it will take, and the lease bounds that on the server side anyway.
        KillSignal = "SIGTERM";
        TimeoutStopSec = 600;
        KillMode = "mixed";
        # A desktop's owner comes first: the agent and the model run at low priority.
        Nice = 10;
        IOSchedulingClass = "best-effort";
        IOSchedulingPriority = 6;
      } // sandbox;
      unitConfig.StartLimitIntervalSec = 0;
    };

    systemd.services.mindprint-render-updater = {
      description = "Mindprint render node updater (pulls the release the server advertises)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      inherit environment path;
      serviceConfig = {
        Type = "oneshot";
        User = "mindprint-render";
        Group = "mindprint-render";
        WorkingDirectory = home;
        ExecStart = "${python}/bin/python3 ${updater}";
        # Building a runtime environment and self-testing it can take a while on first install.
        TimeoutStartSec = "2h";
      } // sandbox;
    };

    systemd.timers.mindprint-render-updater = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = cfg.updateEvery;
        RandomizedDelaySec = "30s";
        Unit = "mindprint-render-updater.service";
      };
    };
  };
}
