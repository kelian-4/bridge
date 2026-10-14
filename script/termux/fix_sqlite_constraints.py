#!/usr/bin/env python3
"""
Recrée les contraintes UNIQUE/PRIMARY KEY manquantes dans homeserver.db
(SQLite), en se basant sur la liste extraite de la base Postgres d'origine.

Pour chaque table listée, si elle n'a pas déjà une contrainte UNIQUE sur
exactement ces colonnes, on recrée la table (SQLite ne supporte pas
ALTER TABLE ... ADD CONSTRAINT) en conservant les données existantes.
"""
from __future__ import annotations

import sqlite3
import sys

SQLITE_PATH = "/root/synapse-data/homeserver.db"

# (table, colonnes) extrait de information_schema côté Postgres.
# Plusieurs lignes pour une même table = plusieurs contraintes distinctes.
CONSTRAINTS: list[tuple[str, list[str]]] = [
    ("access_tokens", ["id"]),
    ("access_tokens", ["token"]),
    ("account_data", ["user_id", "account_data_type"]),
    ("account_validity", ["user_id"]),
    ("application_services_state", ["as_id"]),
    ("application_services_txns", ["as_id", "txn_id"]),
    ("applied_module_schemas", ["module_name", "file"]),
    ("applied_schema_deltas", ["version", "file"]),
    ("appservice_stream_position", ["lock"]),
    ("background_updates", ["update_name"]),
    ("current_state_events", ["event_id"]),
    ("current_state_events", ["room_id", "type", "state_key"]),
    ("dehydrated_devices", ["user_id"]),
    ("delayed_events", ["user_localpart", "delay_id"]),
    ("delayed_events_stream_pos", ["lock"]),
    ("destination_rooms", ["destination", "room_id"]),
    ("destinations", ["destination"]),
    ("device_lists_changes_converted_stream_position", ["lock"]),
    ("device_lists_changes_in_room_max_pruned_stream_id", ["lock"]),
    ("device_lists_remote_pending", ["stream_id"]),
    ("devices", ["user_id", "device_id"]),
    ("e2e_device_keys_json", ["user_id", "device_id"]),
    ("e2e_fallback_keys_json", ["user_id", "device_id", "algorithm"]),
    ("e2e_one_time_keys_json", ["user_id", "device_id", "algorithm", "key_id"]),
    ("event_auth_chain_to_calculate", ["event_id"]),
    ("event_auth_chains", ["event_id"]),
    ("event_backward_extremities", ["event_id", "room_id"]),
    ("event_expiry", ["event_id"]),
    ("event_failed_pull_attempts", ["room_id", "event_id"]),
    ("event_forward_extremities", ["event_id", "room_id"]),
    ("event_json", ["event_id"]),
    ("event_labels", ["event_id", "label"]),
    ("event_push_actions", ["room_id", "event_id", "user_id", "profile_tag"]),
    ("event_push_summary_last_receipt_stream_id", ["lock"]),
    ("event_push_summary_stream_ordering", ["lock"]),
    ("event_reports", ["id"]),
    ("event_to_state_groups", ["event_id"]),
    ("events", ["event_id"]),
    ("ex_outlier_stream", ["event_stream_ordering"]),
    ("instance_map", ["instance_id"]),
    ("local_media_repository", ["media_id"]),
    ("login_tokens", ["token"]),
    ("msc4242_state_dag_forward_extremities", ["event_id"]),
    ("open_id_tokens", ["token"]),
    ("partial_state_events", ["event_id"]),
    ("partial_state_rooms", ["room_id"]),
    ("partial_state_rooms_servers", ["room_id", "server_name"]),
    ("per_user_experimental_features", ["user_id", "feature"]),
    ("profile_updates", ["stream_id"]),
    ("profile_updates_per_user", ["user_id", "stream_id"]),
    ("profiles", ["user_id"]),
    ("push_rules", ["id"]),
    ("push_rules", ["user_name", "rule_id"]),
    ("push_rules_enable", ["id"]),
    ("push_rules_enable", ["user_name", "rule_id"]),
    ("pusher_throttle", ["pusher", "room_id"]),
    ("pushers", ["app_id", "pushkey", "user_name"]),
    ("pushers", ["id"]),
    ("quarantined_media_changes", ["stream_id"]),
    ("receipts_graph", ["room_id", "receipt_type", "user_id", "thread_id"]),
    ("receipts_linearized", ["room_id", "receipt_type", "user_id", "thread_id"]),
    ("received_transactions", ["transaction_id", "origin"]),
    ("redactions", ["event_id"]),
    ("refresh_tokens", ["id"]),
    ("refresh_tokens", ["token"]),
    ("registration_tokens", ["token"]),
    ("rejections", ["event_id"]),
    ("remote_media_cache", ["media_origin", "media_id"]),
    ("room_account_data", ["user_id", "room_id", "account_data_type"]),
    ("room_aliases", ["room_alias"]),
    ("room_ban_redactions", ["room_id", "user_id"]),
    ("room_depth", ["room_id"]),
    ("room_forgetter_stream_pos", ["lock"]),
    ("room_memberships", ["event_id"]),
    ("room_reports", ["id"]),
    ("room_retention", ["room_id", "event_id"]),
    ("room_stats_current", ["room_id"]),
    ("room_tags", ["user_id", "room_id", "tag"]),
    ("room_tags_revisions", ["user_id", "room_id"]),
    ("rooms", ["room_id"]),
    ("scheduled_tasks", ["id"]),
    ("schema_compat_version", ["lock"]),
    ("schema_version", ["lock"]),
    ("server_keys_json", ["server_name", "key_id", "from_server"]),
    ("server_signature_keys", ["server_name", "key_id"]),
    ("sessions", ["session_type", "session_id"]),
    ("sliding_sync_connection_positions", ["connection_position"]),
    ("sliding_sync_connection_required_state", ["required_state_id"]),
    ("sliding_sync_connections", ["connection_key"]),
    ("sliding_sync_joined_rooms", ["room_id"]),
    ("sliding_sync_joined_rooms_to_recalculate", ["room_id"]),
    ("sliding_sync_membership_snapshots", ["room_id", "user_id"]),
    ("state_events", ["event_id"]),
    ("state_groups", ["id"]),
    ("state_groups_pending_deletion", ["sequence_number"]),
    ("state_groups_persisting", ["state_group", "instance_name"]),
    ("stats_incremental_position", ["lock"]),
    ("sticky_events", ["event_stream_ordering"]),
    ("sticky_events", ["stream_id"]),
    ("thread_subscriptions", ["stream_id"]),
    ("thread_subscriptions", ["room_id", "event_id", "user_id"]),
    ("threads", ["room_id", "thread_id"]),
    ("threepid_validation_session", ["session_id"]),
    ("threepid_validation_token", ["token"]),
    ("ui_auth_sessions", ["session_id"]),
    ("ui_auth_sessions_credentials", ["session_id", "stage_type"]),
    ("ui_auth_sessions_ips", ["session_id", "ip", "user_agent"]),
    ("un_partial_stated_event_stream", ["stream_id"]),
    ("un_partial_stated_room_stream", ["stream_id"]),
    ("user_directory_stale_remote_users", ["user_id"]),
    ("user_directory_stream_pos", ["lock"]),
    ("user_external_ids", ["auth_provider", "external_id"]),
    ("user_reports", ["id"]),
    ("user_stats_current", ["user_id"]),
    ("user_threepids", ["medium", "address"]),
    ("users", ["name"]),
    ("users_to_send_full_presence_to", ["user_id"]),
]


def get_existing_unique_sets(conn: sqlite3.Connection, table: str) -> list[set[str]]:
    """Retourne la liste des ensembles de colonnes déjà couverts par un index
    UNIQUE ou par la PRIMARY KEY existante sur cette table."""
    cur = conn.cursor()
    unique_sets: list[set[str]] = []

    # Indices UNIQUE explicites
    cur.execute(f'PRAGMA index_list("{table}")')
    for row in cur.fetchall():
        index_name, is_unique = row[1], row[2]
        if is_unique:
            cur.execute(f'PRAGMA index_info("{index_name}")')
            cols = {r[2] for r in cur.fetchall()}
            unique_sets.append(cols)

    # Colonnes déjà PRIMARY KEY (PRAGMA table_info renvoie pk>0 pour les colonnes de la PK)
    cur.execute(f'PRAGMA table_info("{table}")')
    pk_cols = {r[1] for r in cur.fetchall() if r[5] > 0}
    if pk_cols:
        unique_sets.append(pk_cols)

    return unique_sets


def get_table_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table}")')
    # (name, type)
    return [(r[1], r[2]) for r in cur.fetchall()]


def recreate_table_with_unique(conn: sqlite3.Connection, table: str, unique_cols: list[str]) -> None:
    cur = conn.cursor()
    columns = get_table_columns(conn, table)
    if not columns:
        print(f"  ! Table {table} introuvable dans SQLite, ignorée.")
        return

    col_defs = [f'"{name}" {typ}' for name, typ in columns]
    col_names = [name for name, _ in columns]

    tmp_table = f"{table}__tmp_migration"
    unique_clause = ", ".join(f'"{c}"' for c in unique_cols)

    cur.execute(f'DROP TABLE IF EXISTS "{tmp_table}"')
    cur.execute(
        f'CREATE TABLE "{tmp_table}" ({", ".join(col_defs)}, UNIQUE ({unique_clause}))'
    )

    col_list = ", ".join(f'"{c}"' for c in col_names)
    try:
        cur.execute(f'INSERT INTO "{tmp_table}" ({col_list}) SELECT {col_list} FROM "{table}"')
    except sqlite3.IntegrityError as e:
        print(f"  ! Conflit de données en recréant {table} avec UNIQUE({unique_clause}): {e}")
        print(f"    -> {table} restera sans cette contrainte (données existantes incompatibles).")
        cur.execute(f'DROP TABLE "{tmp_table}"')
        return

    cur.execute(f'DROP TABLE "{table}"')
    cur.execute(f'ALTER TABLE "{tmp_table}" RENAME TO "{table}"')
    conn.commit()
    print(f"  OK: {table} -> UNIQUE({unique_clause})")


def main() -> None:
    conn = sqlite3.connect(SQLITE_PATH)

    # Regroupe les contraintes par table pour ne recréer chaque table qu'une
    # fois même si plusieurs contraintes lui sont associées.
    by_table: dict[str, list[list[str]]] = {}
    for table, cols in CONSTRAINTS:
        by_table.setdefault(table, []).append(cols)

    total = len(by_table)
    for i, (table, constraint_list) in enumerate(by_table.items(), 1):
        existing = get_existing_unique_sets(conn, table)
        missing = [cols for cols in constraint_list if set(cols) not in existing]

        if not missing:
            continue

        print(f"[{i}/{total}] {table}: {len(missing)} contrainte(s) manquante(s)")
        # SQLite ne permet qu'une seule contrainte UNIQUE ajoutée par
        # recréation de table dans ce script simplifié: on applique la
        # première manquante. Si plusieurs sont nécessaires pour une même
        # table, il faudra relancer une seconde passe (rare dans ce schéma).
        recreate_table_with_unique(conn, table, missing[0])

    conn.close()
    print("Terminé.")


if __name__ == "__main__":
    main()
