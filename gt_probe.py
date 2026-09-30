import requests, json, time
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
s=requests.Session(); s.headers.update(UA)
r=s.get("https://trends.google.com/trending/rss?geo=US",timeout=20); print("rss",r.status_code,len(r.text))
r=s.get("https://trends.google.com/?geo=US",timeout=20); print("home",r.status_code, list(s.cookies.keys()))
req={"comparisonItem":[{"keyword":"ai agents","geo":"","time":"today 12-m"}],"category":0,"property":""}
r=s.get("https://trends.google.com/trends/api/explore",params={"hl":"en-US","tz":"0","req":json.dumps(req)},timeout=20)
print("explore",r.status_code,r.text[:200])
if r.status_code==200:
    w=json.loads(r.text[5:])["widgets"][0]
    r2=s.get("https://trends.google.com/trends/api/widgetdata/multiline",params={"hl":"en-US","tz":"0","req":json.dumps(w["request"]),"token":w["token"]},timeout=20)
    print("multiline",r2.status_code,r2.text[:300])
