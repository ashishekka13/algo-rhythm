-- Example BigQuery queries on the algo-rhythm export (python -m algo_rhythm.analytics export --project P).
-- Replace `P.algo_rhythm` with your project and dataset. Plays are keyed by (party_id, position); feedback uses the same.

-- 1. Guest feedback vs Gemini's score, by transition type (blend / echo-out)
WITH mix_votes AS (
  SELECT party_id, position, COUNTIF(value = 'up') AS smooth_votes, COUNTIF(value = 'down') AS rough_votes
  FROM `P.algo_rhythm.feedback` WHERE target = 'transition' GROUP BY party_id, position
)
SELECT p.transition_type,
       COUNT(*) AS joins,
       ROUND(AVG(r.score), 2) AS avg_gemini_score,
       SUM(v.smooth_votes) AS smooth_votes,
       SUM(v.rough_votes) AS rough_votes,
       ROUND(SAFE_DIVIDE(SUM(v.smooth_votes), SUM(v.smooth_votes) + SUM(v.rough_votes)), 2) AS guest_smooth_share
FROM `P.algo_rhythm.plays` p
LEFT JOIN mix_votes v USING (party_id, position)
LEFT JOIN `P.algo_rhythm.transition_reviews` r ON r.play_id = p.id
WHERE p.transition_type IN ('blend', 'echo-out')
GROUP BY p.transition_type;

-- 2. Songs with the most "Loving it" votes
SELECT p.song_name,
       COUNTIF(f.value = 'up') AS loving_it,
       COUNTIF(f.value = 'down') AS not_for_me,
       COUNT(DISTINCT CONCAT(CAST(p.party_id AS STRING), '-', CAST(p.position AS STRING))) AS times_played
FROM `P.algo_rhythm.feedback` f
JOIN `P.algo_rhythm.plays` p USING (party_id, position)
WHERE f.target = 'song'
GROUP BY p.song_name
ORDER BY loving_it DESC, not_for_me ASC
LIMIT 20;

-- 3. Transitions where guests and Gemini disagree (Gemini 4-5 but guests mostly "Rough mix", or Gemini 1-2 but mostly "Smooth mix")
WITH mix_votes AS (
  SELECT party_id, position, COUNTIF(value = 'up') AS smooth_votes, COUNTIF(value = 'down') AS rough_votes
  FROM `P.algo_rhythm.feedback` WHERE target = 'transition' GROUP BY party_id, position
)
SELECT p.party_id, p.position, p.song_name AS into_song, p.transition_type, r.score AS gemini_score, r.issues,
       r.summary, v.smooth_votes, v.rough_votes, p.clip
FROM `P.algo_rhythm.plays` p
JOIN mix_votes v USING (party_id, position)
JOIN `P.algo_rhythm.transition_reviews` r ON r.play_id = p.id
WHERE (r.score >= 4 AND v.rough_votes > v.smooth_votes)
   OR (r.score <= 2 AND v.smooth_votes > v.rough_votes)
ORDER BY ABS(r.score - 3) DESC, v.smooth_votes + v.rough_votes DESC;

-- 4. Request hit rate: requests that matched a library song and that song was then played at the same party
WITH req AS (
  SELECT q.id, q.party_id, q.song_id, LOGICAL_OR(p.id IS NOT NULL) AS played
  FROM `P.algo_rhythm.song_requests` q
  LEFT JOIN `P.algo_rhythm.plays` p
    ON p.party_id = q.party_id AND p.song_id = q.song_id AND p.played_at >= q.created_at
  GROUP BY q.id, q.party_id, q.song_id
)
SELECT party_id,
       COUNT(*) AS requests,
       COUNTIF(song_id IS NOT NULL) AS matched,
       COUNTIF(played) AS played,
       ROUND(SAFE_DIVIDE(COUNTIF(played), COUNT(*)), 2) AS hit_rate
FROM req
GROUP BY party_id
ORDER BY party_id;

-- 5. Energy level vs song feedback: does the crowd like some levels more?
SELECT p.level,
       COUNTIF(f.value = 'up') AS loving_it,
       COUNTIF(f.value = 'down') AS not_for_me,
       ROUND(SAFE_DIVIDE(COUNTIF(f.value = 'up'), COUNT(*)), 2) AS up_share,
       ROUND(AVG(p.energy), 2) AS avg_song_energy
FROM `P.algo_rhythm.feedback` f
JOIN `P.algo_rhythm.plays` p USING (party_id, position)
WHERE f.target = 'song'
GROUP BY p.level
ORDER BY CASE p.level WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 3 ELSE 4 END;

-- 6. Gemini's most common transition issues, by transition type
SELECT p.transition_type, issue, COUNT(*) AS n
FROM `P.algo_rhythm.transition_reviews` r
JOIN `P.algo_rhythm.plays` p ON p.id = r.play_id, UNNEST(r.issues) AS issue
WHERE issue != 'none'
GROUP BY p.transition_type, issue
ORDER BY n DESC;
