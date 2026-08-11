## Event semantics (nba.pbp_event.eventmsgtype)

1  = made field goal      2  = missed field goal   3  = free throw
4  = rebound              5  = turnover            6  = foul
7  = violation            8  = substitution        9  = timeout
10 = jump ball            11 = ejection            12 = period start
13 = period end

On a made field goal (eventmsgtype = 1), `player1_name` is the scorer and
`player2_name` is the assisting player when there was an assist. Assists exist
ONLY here -- there is no assists column anywhere.

Free throws (eventmsgtype = 3) record makes and misses in the description text, not
in a flag column: a MISSED free throw has a description beginning with 'MISS'. Use
COALESCE(homedescription, visitordescription, neutraldescription) to read it,
because which column is populated depends on which team the event belongs to.

`player1_id` usually identifies a player but holds a TEAM id on team-level events
(team rebounds, timeouts), where `player1_name` is NULL. Never assume it resolves to
a player. Unused person2/person3 slots are 0, not NULL.

## Joining the two tables

nba.shot_detail.(game_id, game_event_id) = nba.pbp_event.(game_id, eventnum)

nba.pbp_event has NO date column. To filter play-by-play by date, join through
nba.shot_detail or go via game_id.

