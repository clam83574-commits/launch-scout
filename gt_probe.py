import time, sys
sys.path.insert(0, ".")
from sources.gtrends import Trends, summarize, Blocked
terms = ["ai agents","humanoid robot","ai sdr","vertical saas","clinical trial recruitment","remote patient monitoring","ai code review","vibe coding","voice ai","ai tutor","carbon accounting","defense drones","stablecoin payments","ai legal assistant","construction software","ev charging","insurtech","wealthtech","creator economy","ai video generation","llm observability","synthetic data","robotic surgery","longevity clinic","ai recruiting","expense management","cybersecurity ai","quantum computing","nuclear fusion","agentic commerce","mcp server","ai customer support","edtech","femtech","pet tech"]
t = Trends(pause=float(sys.argv[1]) if len(sys.argv) > 1 else 3)
t0 = time.time()
for i, term in enumerate(terms):
    try:
        s = summarize(t.weekly(term))
        print(i, term, "ok", {k: v for k, v in (s or {}).items() if k != "weekly"}, round(time.time()-t0))
    except Blocked as e:
        print(i, term, "BLOCKED", round(time.time()-t0)); break
    except Exception as e:
        print(i, term, "ERR", e)
