CREATE TABLE nba.pbp_event (
    season                    SMALLINT NOT NULL,  -- season start year; 2023 = the 2023-24 season
    season_type               TEXT     NOT NULL,  -- 'regular' | 'playoffs'
    game_id                   BIGINT   NOT NULL,
    eventnum                  INTEGER  NOT NULL,
    eventmsgtype              SMALLINT NOT NULL,  -- event category; see below
    eventmsgactiontype        SMALLINT,
    period                    SMALLINT NOT NULL,
    wctimestring              TEXT,               -- wall-clock time
    pctimestring              TEXT,               -- game clock remaining, 'MM:SS' text
    homedescription           TEXT,
    neutraldescription        TEXT,
    visitordescription        TEXT,
    score                     TEXT,               -- 'away - home' snapshot, NULL on non-scoring events
    scoremargin               TEXT,               -- signed integer OR the literal 'TIE'; NULL on non-scoring events
    person1type               SMALLINT,
    player1_id                BIGINT,
    player1_name              TEXT,
    player1_team_id           BIGINT,
    player1_team_city         TEXT,
    player1_team_nickname     TEXT,
    player1_team_abbreviation TEXT,
    person2type               SMALLINT,
    player2_id                BIGINT,
    player2_name              TEXT,
    player2_team_id           BIGINT,
    player2_team_city         TEXT,
    player2_team_nickname     TEXT,
    player2_team_abbreviation TEXT,
    person3type               SMALLINT,
    player3_id                BIGINT,
    player3_name              TEXT,
    player3_team_id           BIGINT,
    player3_team_city         TEXT,
    player3_team_nickname     TEXT,
    player3_team_abbreviation TEXT,
    video_available_flag      SMALLINT,
    PRIMARY KEY (game_id, eventnum)
);

CREATE TABLE nba.shot_detail (
    season            SMALLINT NOT NULL,
    season_type       TEXT     NOT NULL,
    game_id           BIGINT   NOT NULL,
    game_event_id     INTEGER  NOT NULL,  -- matches pbp_event.eventnum within the same game_id
    player_id         BIGINT   NOT NULL,
    player_name       TEXT,
    team_id           BIGINT   NOT NULL,
    team_name         TEXT,
    period            SMALLINT NOT NULL,
    minutes_remaining SMALLINT,
    seconds_remaining SMALLINT,
    event_type        TEXT,               -- 'Made Shot' | 'Missed Shot'
    action_type       TEXT,               -- e.g. 'Jump Shot', 'Driving Layup Shot'
    shot_type         TEXT,               -- '2PT Field Goal' | '3PT Field Goal'
    shot_zone_basic   TEXT,               -- e.g. 'Restricted Area', 'Above the Break 3', 'Left Corner 3'
    shot_zone_area    TEXT,
    shot_zone_range   TEXT,
    shot_distance     SMALLINT,           -- feet
    loc_x             SMALLINT,           -- tenths of a foot, origin at the basket, + = right
    loc_y             SMALLINT,           -- tenths of a foot, origin at the basket, + = away from baseline
    shot_made_flag    SMALLINT NOT NULL,  -- 0 | 1
    game_date         DATE     NOT NULL,
    htm               TEXT,               -- home team abbreviation
    vtm               TEXT,               -- visiting team abbreviation
    PRIMARY KEY (game_id, game_event_id)
);
