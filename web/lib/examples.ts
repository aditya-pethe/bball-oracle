// Every query verified to return rows against the live 2023-24 season through
// sandbox_ro with the production 5s timeout — see .agents/p2_sandbox_ui.md log.
export type Example = { title: string; sql: string };

export const EXAMPLES: Example[] = [
  {
    title: "Top scorers by made field goals",
    sql: `SELECT player_name,
       SUM(CASE WHEN shot_type = '3PT Field Goal' THEN 3 ELSE 2 END) AS fg_points,
       COUNT(*) AS makes
FROM nba.shot_detail
WHERE shot_made_flag = 1
GROUP BY player_name
ORDER BY fg_points DESC
LIMIT 15`,
  },
  {
    title: "Corner-three accuracy (min. 50 attempts)",
    sql: `SELECT player_name,
       COUNT(*) AS attempts,
       ROUND(AVG(shot_made_flag) * 100, 1) AS fg_pct
FROM nba.shot_detail
WHERE shot_zone_basic IN ('Left Corner 3', 'Right Corner 3')
GROUP BY player_name
HAVING COUNT(*) >= 50
ORDER BY fg_pct DESC
LIMIT 15`,
  },
  {
    title: "Stephen Curry's shot profile by zone",
    sql: `SELECT shot_zone_basic,
       COUNT(*) AS attempts,
       SUM(shot_made_flag) AS makes,
       ROUND(AVG(shot_made_flag) * 100, 1) AS fg_pct
FROM nba.shot_detail
WHERE player_name = 'Stephen Curry'
GROUP BY shot_zone_basic
ORDER BY attempts DESC`,
  },
  {
    title: "Biggest lead swings in a game",
    sql: `SELECT game_id,
       MAX(CASE WHEN scoremargin <> 'TIE' THEN scoremargin::int END) AS biggest_home_lead,
       MIN(CASE WHEN scoremargin <> 'TIE' THEN scoremargin::int END) AS biggest_visitor_lead
FROM nba.pbp_event
WHERE scoremargin IS NOT NULL
GROUP BY game_id
ORDER BY MAX(CASE WHEN scoremargin <> 'TIE' THEN scoremargin::int END)
       - MIN(CASE WHEN scoremargin <> 'TIE' THEN scoremargin::int END) DESC
LIMIT 10`,
  },
  {
    title: "Clutch buckets: final minute, one-possession game",
    sql: `SELECT game_id, period, pctimestring, score, scoremargin,
       COALESCE(homedescription, visitordescription) AS play
FROM nba.pbp_event
WHERE eventmsgtype = 1
  AND period >= 4
  AND pctimestring ~ '^0:'
  AND scoremargin IN ('TIE', '1', '-1', '2', '-2', '3', '-3')
ORDER BY game_id, eventnum
LIMIT 100`,
  },
  {
    title: "Deepest made shots of the season",
    sql: `SELECT player_name, shot_distance, action_type, game_date, htm, vtm
FROM nba.shot_detail
WHERE shot_made_flag = 1
ORDER BY shot_distance DESC
LIMIT 20`,
  },
  {
    title: "Most threes by a team in one game",
    sql: `SELECT game_id, team_name, COUNT(*) AS threes_made
FROM nba.shot_detail
WHERE shot_made_flag = 1 AND shot_type = '3PT Field Goal'
GROUP BY game_id, team_name
ORDER BY threes_made DESC
LIMIT 10`,
  },
  {
    title: "Deep makes with their play-by-play call (join)",
    sql: `SELECT s.player_name, s.shot_distance,
       COALESCE(p.homedescription, p.visitordescription) AS play
FROM nba.shot_detail s
JOIN nba.pbp_event p
  ON p.game_id = s.game_id AND p.eventnum = s.game_event_id
WHERE s.shot_made_flag = 1 AND s.shot_distance >= 30
ORDER BY s.shot_distance DESC
LIMIT 25`,
  },
];
