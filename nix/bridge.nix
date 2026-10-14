{ inputs, config, pkgs, ... }:
{

  services.postgresql = {
    enable = true;
    package = pkgs.postgresql_16;
    ensureDatabases = [ "synapse" ];
    ensureUsers = [
      {
        name = "synapse";
        ensureDBOwnership = true;
      }
    ];
    authentication = pkgs.lib.mkOverride 10 ''
      local synapse synapse peer map=synapse-map
      local all all peer
    '';
    identMap = ''
      synapse-map matrix-synapse synapse
      synapse-map root synapse
      synapse-map synapse synapse
    '';
  };

  services.matrix-synapse = {
  enable = true;
  settings = {
    server_name = "local.bridge";
    public_baseurl = "http://localhost:8008";
    listeners = [
      {
        port = 8008;
        bind_addresses = [ "127.0.0.1" ];
        type = "http";
        tls = false;
        x_forwarded = false;
        resources = [
          { names = [ "client" "federation" ]; compress = false; }
        ];
      }
    ];
    database = {
      name = "psycopg2";
      args = {
        user = "synapse";
        database = "synapse";
        host = "/run/postgresql";
      };
    };
    federation_domain_whitelist = [ ];
    enable_registration = false;
    registration_shared_secret_path = "/var/lib/matrix-synapse/registration_secret";
  };
};

  services.mautrix-whatsapp = {
  enable = true;
  settings = {
    homeserver = {
      address = "http://localhost:8008";
      domain = "local.bridge";
    };
    appservice = {
      address = "http://localhost:29318";
      hostname = "127.0.0.1";
      port = 29318;
    };
    database = {
      type = "sqlite3-fk-wal";
      uri = "file:/var/lib/mautrix-whatsapp/mautrix-whatsapp.db?_txlock=immediate";
    };
            bridge = {
      permissions = {
        "local.bridge" = "user";
        "@kelian:local.bridge" = "admin";
      };
      relay = {
        enabled = true;
        message_formats = {
          "m.text" = "{{ .Message }}";
  "m.notice" = "{{ .Message }}";
  "m.emote" = "* {{ .Message }}";
  "m.image" = "{{ .Caption }}";
  "m.audio" = "{{ .Caption }}";
  "m.video" = "{{ .Caption }}";
  "m.file" = "{{ .Caption }}";
        };
      };
    };
    network = {
      os_name = "Mautrix-WhatsApp bridge";
      browser_name = "unknown";
      displayname_template = ''{{or .BusinessName .PushName .Phone .RedactedPhone "Unknown user"}} (WA)'';
      call_start_notices = true;
      identity_change_notices = true;
      send_presence_on_typing = true;
      enable_status_broadcast = false;
      disable_status_broadcast_send = false;
      mute_status_broadcast = true;
      pinned_tag = "m.favourite";
      status_broadcast_tag = "m.lowpriority";
      whatsapp_thumbnail = false;
      url_previews = false;
      extev_polls = false;
      disable_view_once = false;
      force_active_delivery_receipts = true;
      direct_media_auto_request = true;
      initial_auto_reconnect = true;
      use_whatsapp_retry_store = false;
    };
  };
};
       services.mautrix-telegram = {
    enable = true;
    package = pkgs.mautrix-telegram.overrideAttrs (old: {
      patches = (old.patches or [ ]) ++ [
        ./0001-fix-empty-body-media-drop.patch
      ];
    });
    settings = {
      homeserver = {
        address = "http://localhost:8008";
        domain = "local.bridge";
      };
      appservice = {
        address = "http://localhost:29317";
        hostname = "127.0.0.1";
        port = 29317;
        database = "sqlite:////var/lib/mautrix-telegram/mautrix-telegram.db";
      };
      telegram = {
        api_id = 31946952;
        api_hash = "";
        bot_token = "";
      };
      bridge = {
        permissions = {
          "local.bridge" = "user";
          "@kelian:local.bridge" = "admin";
          "*" = "relaybot";
        };
        relaybot = {
          authless_portals = true;
          ignore_unbridged_group_chat = false;
          message_formats = {
            "m.text" = "{{ .Message }}";
  "m.notice" = "{{ .Message }}";
  "m.emote" = "* {{ .Message }}";
  "m.image" = "{{ .Caption }}";
  "m.audio" = "{{ .Caption }}";
  "m.video" = "{{ .Caption }}";
  "m.file" = "{{ .Caption }}";
          };
        };
      };
    };
  };

  services.mautrix-signal = {
  enable = true;
  package = pkgs.mautrix-signal.overrideAttrs (old: rec {
    version = "26.07";
    src = pkgs.fetchFromGitHub {
      owner = "mautrix";
      repo = "signal";
      rev = "v0.2607.0";
      hash = "sha256-l6IIL2bClC6t5+P0/AkFIjkD/eDpQnnmA8x4i5ROaY4=";
    };
    vendorHash = "sha256-0ifGza94s4+ED5OrlrqoDKIDZIYJWJhB2q3LJRpKiJs=";
  });
  settings = {
    homeserver = {
      address = "http://localhost:8008";
      domain = "local.bridge";
    };
    appservice = {
      address = "http://localhost:29328";
      hostname = "127.0.0.1";
      port = 29328;
    };
    database = {
      type = "sqlite3-fk-wal";
      uri = "file:/var/lib/mautrix-signal/mautrix-signal.db?_txlock=immediate";
    };
        bridge = {
      permissions = {
        "local.bridge" = "user";
        "@kelian:local.bridge" = "admin";
      };
      relay = {
        enabled = true;
message_formats = {
  ""         = "";
  "m.text"   = "<b>{{ or .Sender.Displayname .Sender.UserID }}</b>: {{ .Message }}";
  "m.notice" = "<b>{{ or .Sender.Displayname .Sender.UserID }}</b>: {{ .Message }}";
  "m.emote"  = "* <b>{{ or .Sender.Displayname .Sender.UserID }}</b> {{ .Message }}";
  "m.image"  = "<b>{{ or .Sender.Displayname .Sender.UserID }}</b>{{ if .Caption }}: {{ .Caption }}{{ end }}";
  "m.audio"  = "<b>{{ or .Sender.Displayname .Sender.UserID }}</b>{{ if .Caption }}: {{ .Caption }}{{ end }}";
  "m.video"  = "<b>{{ or .Sender.Displayname .Sender.UserID }}</b>{{ if .Caption }}: {{ .Caption }}{{ end }}";
  "m.file"   = "<b>{{ or .Sender.Displayname .Sender.UserID }}</b>{{ if .Caption }}: {{ .Caption }}{{ end }}";
};
      };
    };
    network = {
      displayname_template = "{{or .ProfileName .PhoneNumber \"Unknown user\"}}";
      use_contact_avatars = true;
      sync_contacts_on_startup = true;
      use_outdated_profiles = false;
      number_in_topic = true;
      device_name = "mautrix-signal";
      location_format = "https://www.google.com/maps/place/%[1]s,%[2]s";
      disappear_view_once = false;
      extev_polls = false;
    };
  };
};
}
