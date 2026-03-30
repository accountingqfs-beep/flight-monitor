#!/usr/bin/env python3
"""
Flight Price Monitor v3.2
- Synced dashboard IDs
- Auto-commits price_data.json to repo for dashboard auto-fetch
"""
import os,sys,json,base64,logging
from datetime import datetime,date
from pathlib import Path
try:
    import requests
except ImportError:
    import subprocess;subprocess.check_call([sys.executable,"-m","pip","install","requests","-q"]);import requests

CONFIG={
    "api_key":os.environ.get("SERPAPI_KEY",""),
    "discord_webhook_url":os.environ.get("DISCORD_WEBHOOK",""),
    "data_dir":Path(os.environ.get("DATA_DIR",str(Path.home()/".flight_monitor"))),
    "github_token":os.environ.get("GH_TOKEN",""),
    "repo":"accountingqfs-beep/flight-monitor",
}

# ═══ LOAD TRIPS FROM trips.json ═══
CABIN_MAP={"economy":"1","premium":"2","business":"3","first":"4"}
STOPS_MAP={"nonstop":"0","1stop":"1","any":"2"}

def load_trips():
    """Load trip definitions from trips.json in repo root (same dir as script)."""
    p=Path(__file__).parent/"trips.json"
    if not p.exists():
        log.error("trips.json not found");return []
    raw=json.load(open(p))
    trips=[]
    for t in raw:
        if not t.get("ac") or t.get("archived"):continue
        trips.append({
            "id":t["id"],"name":t.get("nm",""),
            "origin":t["or"],"destination":t["ds"],
            "outbound_date":t["od"],"return_date":t.get("rd",""),
            "passengers":t.get("px",1),
            "stops":STOPS_MAP.get(t.get("st","any"),"2"),
            "cabin":CABIN_MAP.get(t.get("cb","economy"),"1"),
            "booked_price":t.get("bp") or None,
            "alert_threshold":t.get("at",100),
            "active":True,
            "airline_filter":t.get("af",""),
            "search_one_ways":t.get("ow",False),
            "outbound_airline":t.get("oa",""),
            "return_airline":t.get("ra",""),
        })
    return trips

TRIPS=load_trips()

def setup_logging():
    d=CONFIG["data_dir"];d.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(d/"monitor.log"),logging.StreamHandler(sys.stdout)])
    return logging.getLogger(__name__)
log=setup_logging()

def serpapi_search(params):
    params["api_key"]=CONFIG["api_key"];params["engine"]="google_flights";params["currency"]="USD";params["hl"]="en"
    params={k:v for k,v in params.items() if v}
    resp=requests.get("https://serpapi.com/search",params=params,timeout=45);resp.raise_for_status();data=resp.json()
    flights=[]
    for cat in ["best_flights","other_flights"]:
        for fg in data.get(cat,[]):
            entry={"price":fg.get("price"),"duration":fg.get("total_duration"),"airline":None,"flight_numbers":[],"is_basic":False}
            for seg in fg.get("flights",[]):
                entry["flight_numbers"].append(seg.get("flight_number",""))
                if not entry["airline"]:entry["airline"]=seg.get("airline","")
            for ext in fg.get("extensions",[]):
                if isinstance(ext,str) and "Basic" in ext:entry["is_basic"]=True
            if entry["price"]:flights.append(entry)
    return {"flights":flights,"price_insights":data.get("price_insights",{}),"status":data.get("search_metadata",{}).get("status","unknown")}

def find_lowest(fl):
    if not fl:return None
    f=min(fl,key=lambda x:x["price"])
    return {"price":f["price"],"duration":f.get("duration"),"airline":f.get("airline"),"flight_numbers":f.get("flight_numbers",[])}

def find_shortest(fl,tol=30):
    if not fl:return None
    valid=[f for f in fl if f.get("duration")]
    if not valid:return find_lowest(fl)
    md=min(f["duration"] for f in valid);sg=[f for f in valid if f["duration"]<=md+tol]
    f=min(sg,key=lambda x:x["price"])
    return {"price":f["price"],"duration":f.get("duration"),"airline":f.get("airline"),"flight_numbers":f.get("flight_numbers",[])}

def check_trip(trip):
    results={"timestamp":datetime.utcnow().isoformat()+"Z","trip_id":trip["id"],"api_calls":0}
    log.info(f"[{trip['id']}] RT: {trip['origin']}->{trip['destination']}")
    rp={"type":"1","departure_id":trip["origin"],"arrival_id":trip["destination"],
        "outbound_date":trip["outbound_date"],"return_date":trip["return_date"],
        "adults":str(trip["passengers"]),"stops":trip.get("stops","0"),"travel_class":trip.get("cabin","1")}
    if trip.get("airline_filter"):rp["include_airlines"]=trip["airline_filter"]
    rt=serpapi_search(rp);results["api_calls"]+=1
    log.info(f"[{trip['id']}] RT results: {len(rt['flights'])} flights, status={rt['status']}")
    # If zero results and stops were restricted, retry with any stops
    if not rt["flights"] and trip.get("stops","0") in ("0","1"):
        log.info(f"[{trip['id']}] Retrying RT with any stops")
        rp["stops"]="2"
        rt=serpapi_search(rp);results["api_calls"]+=1
        log.info(f"[{trip['id']}] RT retry results: {len(rt['flights'])} flights")
    results["rt_status"]=rt["status"];results["rt_num_results"]=len(rt["flights"]);results["price_insights"]=rt["price_insights"]
    results["rt_lowest"]=find_lowest(rt["flights"]);results["rt_shortest"]=find_shortest(rt["flights"])
    results["ow_enabled"]=trip.get("search_one_ways",False);results["ow_combined"]=None
    if trip.get("search_one_ways"):
        log.info(f"[{trip['id']}] OW out")
        op={"type":"2","departure_id":trip["origin"],"arrival_id":trip["destination"],
            "outbound_date":trip["outbound_date"],"adults":str(trip["passengers"]),"stops":trip.get("stops","0"),"travel_class":trip.get("cabin","1")}
        if trip.get("outbound_airline"):op["include_airlines"]=trip["outbound_airline"]
        ow_out=serpapi_search(op);results["api_calls"]+=1
        log.info(f"[{trip['id']}] OW out results: {len(ow_out['flights'])} flights")
        # Retry OW out with any stops if needed
        if not ow_out["flights"] and trip.get("stops","0") in ("0","1"):
            log.info(f"[{trip['id']}] Retrying OW out with any stops")
            op["stops"]="2"
            ow_out=serpapi_search(op);results["api_calls"]+=1
        log.info(f"[{trip['id']}] OW ret")
        rr={"type":"2","departure_id":trip["destination"],"arrival_id":trip["origin"],
            "outbound_date":trip["return_date"],"adults":str(trip["passengers"]),"stops":trip.get("stops","0"),"travel_class":trip.get("cabin","1")}
        if trip.get("return_airline"):rr["include_airlines"]=trip["return_airline"]
        ow_ret=serpapi_search(rr);results["api_calls"]+=1
        log.info(f"[{trip['id']}] OW ret results: {len(ow_ret['flights'])} flights")
        # Retry OW ret with any stops if needed
        if not ow_ret["flights"] and trip.get("stops","0") in ("0","1"):
            log.info(f"[{trip['id']}] Retrying OW ret with any stops")
            rr["stops"]="2"
            ow_ret=serpapi_search(rr);results["api_calls"]+=1
        ol=find_lowest(ow_out["flights"]);rl=find_lowest(ow_ret["flights"])
        if ol and rl:
            results["ow_combined"]={"lowest_total":ol["price"]+rl["price"],"out_price":ol["price"],"out_airline":ol.get("airline",""),"out_duration":ol.get("duration"),"ret_price":rl["price"],"ret_airline":rl.get("airline",""),"ret_duration":rl.get("duration")}
    return results

def validate(r):
    score=100;issues=[]
    if r.get("rt_status") not in ("Success","success"):score-=20;issues.append("not success")
    if r.get("rt_num_results",0)==0:score-=50;issues.append("no flights")
    elif r.get("rt_num_results",0)<3:score-=10
    lp=r.get("rt_lowest",{})
    if not lp or not lp.get("price"):score-=30;issues.append("no price")
    elif lp["price"]<50:score-=25
    g="A" if score>=90 else "B" if score>=75 else "C" if score>=60 else "F"
    return {"score":max(0,score),"grade":g,"issues":issues}

# ═══ PRICE DATA FILE ═══
def load_price_data():
    # Try repo checkout dir first (for GitHub Actions), then data_dir
    repo_p=Path(__file__).parent/"price_data.json"
    if repo_p.exists():
        log.info(f"Loading price data from repo: {repo_p}")
        return json.load(open(repo_p))
    p=CONFIG["data_dir"]/"price_data.json"
    return json.load(open(p)) if p.exists() else {}

def save_price_data(pd):
    with open(CONFIG["data_dir"]/"price_data.json","w") as f:json.dump(pd,f,indent=2,default=str)

def add_check(pd,tid,results):
    if tid not in pd:pd[tid]=[]
    entry={
        "dt":results["timestamp"][:10],
        "ts":results["timestamp"],
        "rtp":results.get("rt_lowest",{}).get("price") if results.get("rt_lowest") else None,
        "rsp":results.get("rt_shortest",{}).get("price") if results.get("rt_shortest") else None,
        "rsd":results.get("rt_shortest",{}).get("duration") if results.get("rt_shortest") else None,
        "owt":results.get("ow_combined",{}).get("lowest_total") if results.get("ow_combined") else None,
        "owop":results.get("ow_combined",{}).get("out_price") if results.get("ow_combined") else None,
        "owoa":results.get("ow_combined",{}).get("out_airline") if results.get("ow_combined") else None,
        "owrp":results.get("ow_combined",{}).get("ret_price") if results.get("ow_combined") else None,
        "owra":results.get("ow_combined",{}).get("ret_airline") if results.get("ow_combined") else None,
        "tr":results.get("price_insights",{}).get("typical_price_range",[]),
        "pl":results.get("price_insights",{}).get("price_level",""),
        "gr":validate(results)["grade"],
        "src":"auto",
    }
    pd[tid].append(entry)
    save_price_data(pd)
    return entry

# ═══ COMMIT TO GITHUB ═══
def commit_price_data(pd):
    token=CONFIG.get("github_token")
    if not token:
        log.warning("GH_TOKEN not set, skipping commit");return False
    repo=CONFIG["repo"];path="price_data.json"
    url=f"https://api.github.com/repos/{repo}/contents/{path}"
    headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github.v3+json"}
    # Get current file sha if it exists
    sha=None
    try:
        r=requests.get(url,headers=headers,timeout=15)
        if r.status_code==200:sha=r.json().get("sha")
    except:pass
    content=base64.b64encode(json.dumps(pd,indent=2,default=str).encode()).decode()
    body={"message":"Update price data","content":content,"branch":"main"}
    if sha:body["sha"]=sha
    try:
        r=requests.put(url,headers=headers,json=body,timeout=15)
        if r.status_code in (200,201):log.info("price_data.json committed to repo");return True
        else:log.error(f"GitHub commit failed {r.status_code}: {r.text[:200]}");return False
    except Exception as e:log.error(f"GitHub commit error: {e}");return False

# ═══ DISCORD ═══
def fmt_dur(m):
    return f"{m//60}h {m%60}m" if m else "?"

def send_discord(trip,results,stats,val):
    url=CONFIG.get("discord_webhook_url")
    if not url:return
    rt_lp=results.get("rt_lowest",{}).get("price") if results.get("rt_lowest") else None
    rt_sp=results.get("rt_shortest",{}).get("price") if results.get("rt_shortest") else None
    rt_sd=results.get("rt_shortest",{}).get("duration") if results.get("rt_shortest") else None
    ow=results.get("ow_combined");pi=results.get("price_insights",{})
    booked=trip.get("booked_price");prices=[p for p in [rt_lp,rt_sp,ow.get("lowest_total") if ow else None] if p]
    best=min(prices) if prices else rt_lp;drop=booked-best if booked and best else None
    if drop and drop>=trip.get("alert_threshold",100):color,pfx=0x10B981,"🟢 DROP"
    elif drop and drop>0:color,pfx=0xF59E0B,"🟡 Small"
    elif drop and drop<0:color,pfx=0xEF4444,"🔴 Up"
    else:color,pfx=0x3B82F6,"✈️ Price check"
    title=f"{pfx} — {trip['origin']} → {trip['destination']}"
    fields=[{"name":"🔄 ROUND TRIP","value":"─────","inline":False}]
    if rt_lp:fields.append({"name":"Lowest","value":f"**${rt_lp}**","inline":True})
    if rt_sp:fields.append({"name":"Shortest","value":f"**${rt_sp}** ({fmt_dur(rt_sd)})","inline":True})
    if booked and rt_lp:d2=booked-rt_lp;fields.append({"name":"vs Booked","value":f"{'↓' if d2>0 else '↑'} **${abs(d2):.0f}**","inline":True})
    if ow:
        fields.append({"name":"🔀 ONE-WAYS","value":"─────","inline":False})
        fields.append({"name":"Out","value":f"${ow['out_price']} ({ow.get('out_airline','?')})","inline":True})
        fields.append({"name":"Ret","value":f"${ow['ret_price']} ({ow.get('ret_airline','?')})","inline":True})
        fields.append({"name":"Total","value":f"**${ow['lowest_total']}**","inline":True})
        if rt_lp:
            diff=rt_lp-ow["lowest_total"]
            if diff>0:fields.append({"name":"💡","value":f"OW saves **${diff:.0f}**","inline":False})
            elif diff<0:fields.append({"name":"📊","value":f"RT saves ${abs(diff):.0f}","inline":False})
    typical=pi.get("typical_price_range",[])
    if typical:fields.append({"name":"Typical","value":f"${typical[0]}-${typical[1]} ({pi.get('price_level','?')})","inline":True})
    if stats:
        ti="📉" if stats["trend"]=="down" else "📈" if stats["trend"]=="up" else "➡️"
        fields.append({"name":"Trend","value":f"{ti} {stats['trend']}","inline":True})
    fields.append({"name":"Grade","value":f"{val['grade']} ({val['score']}/100)","inline":True})
    cabin_label={"1":"Economy","2":"Premium Econ","3":"Business","4":"First"}.get(trip.get("cabin","1"),"Economy")
    embed={"title":title,"color":color,"fields":fields,
        "footer":{"text":f"{trip.get('name','')} · {cabin_label} · {trip['outbound_date']}→{trip.get('return_date','')} · {trip['passengers']}pax · {results.get('api_calls',1)} calls"},
        "timestamp":results["timestamp"]}
    payload={"username":"Flight Monitor","avatar_url":"https://em-content.zobj.net/source/apple/391/airplane_2708-fe0f.png","embeds":[embed]}
    try:
        r=requests.post(url,json=payload,timeout=10)
        if r.status_code in (200,204):log.info(f"[{trip['id']}] Discord ok")
        else:log.error(f"[{trip['id']}] Discord {r.status_code}")
    except Exception as e:log.error(f"[{trip['id']}] Discord fail: {e}")

def get_stats(pd,tid):
    checks=pd.get(tid,[])
    rp=[c["rtp"] for c in checks if c.get("rtp")]
    if not rp:return None
    return {"count":len(checks),"current":rp[-1],"lowest":min(rp),"highest":max(rp),"avg":sum(rp)/len(rp),
        "trend":"down" if len(rp)>=2 and rp[-1]<rp[-2] else "up" if len(rp)>=2 and rp[-1]>rp[-2] else "flat"}

def track_api(calls):
    p=CONFIG["data_dir"]/"api_usage.json"
    u=json.load(open(p)) if p.exists() else {}
    m=datetime.utcnow().strftime("%Y-%m");u[m]=u.get(m,0)+calls
    with open(p,"w") as f:json.dump(u,f,indent=2)
    return u[m]

def process_trip(trip,pd):
    log.info(f"[{trip['id']}] === {trip['name']} ===")
    try:results=check_trip(trip)
    except Exception as e:log.error(f"[{trip['id']}] Error: {e}");return {"trip_id":trip["id"],"error":str(e),"api_calls":0}
    val=validate(results);log.info(f"[{trip['id']}] Grade:{val['grade']} Calls:{results['api_calls']}")
    track_api(results["api_calls"])
    add_check(pd,trip["id"],results)
    stats=get_stats(pd,trip["id"])
    rt_lp=results.get("rt_lowest",{}).get("price") if results.get("rt_lowest") else None
    always=os.environ.get("ALWAYS_NOTIFY","true").lower()=="true"
    if always or (trip.get("booked_price") and rt_lp and trip["booked_price"]-rt_lp>=trip.get("alert_threshold",100)):
        send_discord(trip,results,stats,val)
    return {"trip_id":trip["id"],"rt_lowest":rt_lp,"api_calls":results["api_calls"],"grade":val["grade"]}

def run_all():
    if not CONFIG["api_key"]:log.error("SERPAPI_KEY not set");sys.exit(1)
    active=[t for t in TRIPS if t.get("active") and t.get("outbound_date","")>=date.today().isoformat()]
    log.info(f"Checking {len(active)} trip(s)")
    pd=load_price_data();results=[];total=0
    for trip in active:
        r=process_trip(trip,pd);results.append(r);total+=r.get("api_calls",0)
    # Commit price_data.json to repo
    commit_price_data(pd)
    log.info(f"DONE — {total} API calls")
    for r in results:log.info(f"  {r['trip_id']}: ${r.get('rt_lowest','?')} [{r.get('grade','?')}]")
    return results

if __name__=="__main__":
    import argparse;parser=argparse.ArgumentParser();parser.add_argument("command",choices=["check","status","test"])
    args=parser.parse_args()
    if args.command=="check":print(json.dumps(run_all(),indent=2,default=str))
    elif args.command=="status":
        pd=load_price_data()
        for t in TRIPS:
            s=get_stats(pd,t["id"])
            print(f"{t['origin']}->{t['destination']} ({t['name']}): {s or 'no data'}")
    elif args.command=="test":
        print("Test mode");send_discord(
            {"id":"test","name":"Test","origin":"CVG","destination":"OGG","outbound_date":"2026-11-21","return_date":"2026-11-28","passengers":2,"booked_price":None,"alert_threshold":100,"cabin":"1"},
            {"timestamp":datetime.utcnow().isoformat()+"Z","trip_id":"test","api_calls":3,"rt_status":"Success","rt_num_results":10,
             "rt_lowest":{"price":612,"duration":490,"airline":"Delta"},"rt_shortest":{"price":689,"duration":370,"airline":"Delta"},
             "ow_enabled":True,"ow_combined":{"lowest_total":567,"out_price":289,"out_airline":"Delta","ret_price":278,"ret_airline":"United"},
             "price_insights":{"price_level":"low","typical_price_range":[550,920]}},
            {"count":5,"current":612,"lowest":580,"highest":750,"avg":665,"trend":"down"},
            {"score":92,"grade":"A","issues":[]})
