-- 0001_create_nba_schema.sql
-- Phase 0: base analytics schema for the SQL sandbox.
-- Creates schema `nba` with the two event-grain tables loaded from shufinskiy/nba_data
-- (`nbastats` -> nba.pbp_event, `shotdetail` -> nba.shot_detail).
--
-- Column names are the source CSV headers lowercased verbatim, including the source's own
-- inconsistent casing style (`homedescription` vs `player1_team_id`). Renaming half of them
-- would create a translation layer the ETL has to keep correct for no analytical gain.
--
-- Creating the `sandbox_ro` role and its grants is deliberately NOT in this migration —
-- that is a separate, separately-tested migration (see .agents/p0_datapipeline.md §3).

BEGIN;

CREATE SCHEMA IF NOT EXISTS nba;

COMMENT ON SCHEMA nba IS
  'Read-only NBA event data loaded offline by the Python ETL. Never written to on the request path. Not exposed via PostgREST; the query sandbox reaches it over a direct Postgres connection as sandbox_ro.';


-- ---------------------------------------------------------------------------
-- nba.pbp_event  <- nbastats_<year>.csv / nbastats_po_<year>.csv (34 source columns)
-- ---------------------------------------------------------------------------
CREATE TABLE nba.pbp_event (
    -- Derived at load time from the archive filename, not present in the source CSV.
    season                      SMALLINT  NOT NULL,
    season_type                 TEXT      NOT NULL,

    game_id                     BIGINT    NOT NULL,
    eventnum                    INTEGER   NOT NULL,
    eventmsgtype                SMALLINT  NOT NULL,
    eventmsgactiontype          SMALLINT,
    period                      SMALLINT  NOT NULL,
    wctimestring                TEXT,
    pctimestring                TEXT,
    homedescription             TEXT,
    neutraldescription          TEXT,
    visitordescription          TEXT,
    score                       TEXT,
    scoremargin                 TEXT,

    person1type                 SMALLINT,
    player1_id                  BIGINT,
    player1_name                TEXT,
    player1_team_id             BIGINT,
    player1_team_city           TEXT,
    player1_team_nickname       TEXT,
    player1_team_abbreviation   TEXT,

    person2type                 SMALLINT,
    player2_id                  BIGINT,
    player2_name                TEXT,
    player2_team_id             BIGINT,
    player2_team_city           TEXT,
    player2_team_nickname       TEXT,
    player2_team_abbreviation   TEXT,

    person3type                 SMALLINT,
    player3_id                  BIGINT,
    player3_name                TEXT,
    player3_team_id             BIGINT,
    player3_team_city           TEXT,
    player3_team_nickname       TEXT,
    player3_team_abbreviation   TEXT,

    video_available_flag        SMALLINT,

    CONSTRAINT pbp_event_pkey PRIMARY KEY (game_id, eventnum),
    CONSTRAINT pbp_event_season_type_check CHECK (season_type IN ('regular', 'playoffs'))
);


-- ---------------------------------------------------------------------------
-- nba.shot_detail  <- shotdetail_<year>.csv / shotdetail_po_<year>.csv (24 source columns,
-- minus grid_type and shot_attempted_flag which are constants — see COMMENT below)
-- ---------------------------------------------------------------------------
CREATE TABLE nba.shot_detail (
    season              SMALLINT  NOT NULL,
    season_type         TEXT      NOT NULL,

    game_id             BIGINT    NOT NULL,
    game_event_id       INTEGER   NOT NULL,
    player_id           BIGINT    NOT NULL,
    player_name         TEXT,
    team_id             BIGINT    NOT NULL,
    team_name           TEXT,
    period              SMALLINT  NOT NULL,
    minutes_remaining   SMALLINT,
    seconds_remaining   SMALLINT,
    event_type          TEXT,
    action_type         TEXT,
    shot_type           TEXT,
    shot_zone_basic     TEXT,
    shot_zone_area      TEXT,
    shot_zone_range     TEXT,
    shot_distance       SMALLINT,
    loc_x               SMALLINT,
    loc_y               SMALLINT,
    shot_made_flag      SMALLINT  NOT NULL,
    game_date           DATE      NOT NULL,
    htm                 TEXT,
    vtm                 TEXT,

    CONSTRAINT shot_detail_pkey PRIMARY KEY (game_id, game_event_id),
    CONSTRAINT shot_detail_season_type_check CHECK (season_type IN ('regular', 'playoffs')),
    CONSTRAINT shot_detail_shot_made_flag_check CHECK (shot_made_flag IN (0, 1))
);


-- ---------------------------------------------------------------------------
-- Indexes
--
-- Read-mostly and bulk-loaded, not OLTP: index only the filter columns the Phase 1
-- sandbox actually leads with. game_id is already covered as the leading column of both
-- primary keys. season uses BRIN rather than btree because it has ~5-10 distinct values
-- (a btree would be ignored by the planner at that selectivity) but the table is loaded
-- one season-archive at a time, so season is strongly correlated with physical order —
-- the case BRIN exists for. BRIN costs tens of KB, so it stays cheap even if a per-season
-- reload degrades that correlation.
-- ---------------------------------------------------------------------------
CREATE INDEX pbp_event_player1_id_idx  ON nba.pbp_event (player1_id);
CREATE INDEX pbp_event_player1_team_id_idx ON nba.pbp_event (player1_team_id);
CREATE INDEX pbp_event_season_brin     ON nba.pbp_event USING brin (season) WITH (pages_per_range = 32);

CREATE INDEX shot_detail_player_id_idx ON nba.shot_detail (player_id);
CREATE INDEX shot_detail_team_id_idx   ON nba.shot_detail (team_id);
CREATE INDEX shot_detail_season_date_brin ON nba.shot_detail USING brin (season, game_date) WITH (pages_per_range = 32);


-- ---------------------------------------------------------------------------
-- Comments. These are the user-facing schema docs: the Phase 2 schema browser reads them
-- out of pg_description, so they only cover things a competent SQL user would otherwise
-- get wrong, not every column.
-- ---------------------------------------------------------------------------
COMMENT ON TABLE nba.pbp_event IS
  'One row per play-by-play event (stats.nba.com PBP via nba_data `nbastats`). Join to nba.shot_detail on (game_id, eventnum) = (game_id, game_event_id). Source contains rare byte-identical duplicate rows, so the ETL must de-duplicate before COPY; the primary key enforces that.';

COMMENT ON TABLE nba.shot_detail IS
  'One row per shot attempt with court location and zone (nba_data `shotdetail`). Join to nba.pbp_event on (game_id, game_event_id) = (game_id, eventnum). Source columns grid_type (always ''Shot Chart Detail'') and shot_attempted_flag (always 1) carry no information and are not loaded.';

COMMENT ON COLUMN nba.pbp_event.season IS
  'Season start year (2023 = the 2023-24 season). Derived from the source archive filename at load time; not a source column. Reload unit is (season, season_type).';
COMMENT ON COLUMN nba.pbp_event.season_type IS
  '''regular'' or ''playoffs'', derived from the source archive family (nbastats_<year> vs nbastats_po_<year>).';
COMMENT ON COLUMN nba.pbp_event.game_id IS
  'NBA game id with the leading ''00'' league code stripped, as stored by the source (22300001, not 0022300001). To match an id copied from stats.nba.com use lpad(game_id::text, 10, ''0''); comparing against the string ''0022300001'' also works, since Postgres casts the literal to bigint.';
COMMENT ON COLUMN nba.pbp_event.scoremargin IS
  'Text, not numeric: contains the literal ''TIE'' as well as signed integers. NULL on events that do not change the score.';
COMMENT ON COLUMN nba.pbp_event.score IS
  'Text scoreboard snapshot formatted ''away - home'' (e.g. ''10 - 13''). NULL on events that do not change the score.';
COMMENT ON COLUMN nba.pbp_event.player1_id IS
  'Usually a player id, but holds a TEAM id on team-level events (team rebounds, timeouts), in which case player1_name and player1_team_id are NULL. Do not assume it resolves to a player. Unused person2/person3 slots are 0, not NULL.';
COMMENT ON COLUMN nba.pbp_event.eventmsgtype IS
  'stats.nba.com event category (1=made shot, 2=missed shot, 3=free throw, 4=rebound, 5=turnover, 6=foul, 7=violation, 8=substitution, 9=timeout, 10=jump ball, 11=ejection, 12=period start, 13=period end).';
COMMENT ON COLUMN nba.pbp_event.pctimestring IS
  'Game clock remaining in the period, ''MM:SS'' text as delivered by the source.';

COMMENT ON COLUMN nba.shot_detail.season IS
  'Season start year (2023 = the 2023-24 season). Derived from the source archive filename at load time; not a source column. Reload unit is (season, season_type).';
COMMENT ON COLUMN nba.shot_detail.season_type IS
  '''regular'' or ''playoffs'', derived from the source archive family (shotdetail_<year> vs shotdetail_po_<year>).';
COMMENT ON COLUMN nba.shot_detail.game_id IS
  'NBA game id with the leading ''00'' league code stripped, as stored by the source (22300001, not 0022300001). Same encoding as nba.pbp_event.game_id.';
COMMENT ON COLUMN nba.shot_detail.game_event_id IS
  'Matches nba.pbp_event.eventnum within the same game_id.';
COMMENT ON COLUMN nba.shot_detail.game_date IS
  'Parsed by the ETL from the source''s YYYYMMDD integer. nba.pbp_event has no date column; date-filter play-by-play by joining through this table or through game_id.';
COMMENT ON COLUMN nba.shot_detail.loc_x IS
  'Court x in tenths of a foot, origin at the basket, positive toward the right side of the offensive half court.';
COMMENT ON COLUMN nba.shot_detail.loc_y IS
  'Court y in tenths of a foot, origin at the basket, positive away from the baseline.';

COMMIT;
