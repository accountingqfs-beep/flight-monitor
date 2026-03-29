#!/usr/bin/env python3
"""
Flight Price Monitor v3.1
Synced with dashboard trip IDs.
"""
import os,sys,json,smtplib,logging
from datetime import datetime,date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
try:
    import requests
except ImportError:
    import subprocess;subprocess.check_call([sys.executable,"-m","pip","install","requests","-q"]);import requests

CONFIG={
    "api_key":os.environ.get("SERPAPI_KEY",""),
    "discord_webhook_url":os.environ.get("DISCORD_WEBHOOK",""),
    "email_enabled":os.environ.get("EMAIL_ENABLED","false").lower()=="true",
    "email_smtp_server":"smtp.gmail.com","email_smtp_port":587,
    "email_sender":os.environ.get("EMAIL_SENDER",""),
    "email_password":os.environ.get("EMAIL_PASSWORD",""),
    "email_recipients":[x.strip() for x in os.environ.get("EMAIL_RECIPIENTS","").split(",") if x.strip()],
    "sms_gateway":os.environ.get("SMS_GATEWAY",""),
    "data_dir":Path(os.environ.get("DATA_DIR",str(Path.home()/".flight_monitor"))),
}

# ═══ TRIPS — synced with dashboard IDs ═══
TRIPS=[
    {
        "id":"j2cl8gt",
        "name":"NYC June 2026 - Monnish Wedding (M+M)",
        "origin":"CVG","destination":"LGA",
        "outbound_date":"2026-06-11","return_date":"2026-06-14",
        "passengers":2,"stops":"1","cabin":"2",
        "booked_price":None,"alert_threshold":100,"active":True,
        "airline_filter":"DL",
        "search_one_ways":False,"outbound_airline":"DL","return_airline":"DL",
    },
    {
        "id":"o4xal99",
        "name":"NYC June 2026 - Monnish Wedding (Mom + Pop)",
        "origin":"CVG","destination":"LGA",
        "outbound_date":"2026-06-11","return_date":"2026-06-14",
        "passengers":2,"stops":"1","cabin":"1",
        "booked_price":None,"alert_threshold":100,"active":True,
        "airline_filter":"DL",
        "search_one_ways":False,"outbound_airline":"DL","return_airline":"DL",
    },
    {
        "id":"s6j1q0l",
        "name":"Maui Nov 2026 - Thanksgiving w/Kids",
        "origin":"CVG","destination":"OGG",
        "outbound_date":"2026-11-21","return_date":"2026-11-28",
        "passengers":2,"stops":"2","cabin":"1",
        "booked_price":None,"alert_threshold":100,"active":True,
        "airline_filter":"",
        "search_one_ways":True,"outbound_airline":"","return_airline":"",
    },
]

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
            entry={"price":fg.get("price"),"duration":fg.get("total_duration"),"airline":None,"flight_numbers":[],"is_basic":False,"stops":len(fg.get("flights",[]))-1}
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
    return {"price":f["price"],"duration":f.get("duration"),"airline":f.get("airline"),"flight_numbers":f.get("flight_numbers",[]),"is_basic":f.get("is_basic",False)}

def find_shortest(fl,tol=30):
    if not fl:return None
    valid=[f for f in fl if f.get("duration")]
    if not valid:return find_lowest(fl)
    md=min(f["duration"] for f in valid);sg=[f for f in valid if f["duration"]<=md+tol]
    f=min(sg,key=lambda x:x["price"])
    return {"price":f["price"],"duration":f.get("duration"),"airline":f.get("airline"),"flight_numbers":f.get("flight_numbers",[]),"is_basic":f.get("is_basic",False)}

def check_trip_prices(trip):
    results={"timestamp":datetime.utcnow().isoformat()+"Z","trip_id":trip["id"],"api_calls":0}
    log.info(f"[{trip['id']}] RT: {trip['origin']}->{trip['destination']}")
    rt_params={"type":"1","departure_id":trip["origin"],"arrival_id":trip["destination"],
        "outbound_date":trip["outbound_date"],"return_date":trip["return_date"],
        "adults":str(trip["passengers"]),"stops":trip.get("stops","0"),"travel_class":trip.get("cabin","1")}
    if trip.get("airline_filter"):rt_params["include_airlines"]=trip["airline_filter"]
    rt=serpapi_search(rt_params);results["api_calls"]+=1
    results["rt_status"]=rt["status"];results["rt_num_results"]=len(rt["flights"]);results["price_insights"]=rt["price_insights"]
    results["rt_lowest"]=find_lowest(rt["flights"]);results["rt_shortest"]=find_shortest(rt["flights"])
    results["ow_enabled"]=trip.get("search_one_ways",False);results["ow_combined"]=None;results["ow_combined_shortest"]=None
    if trip.get("search_one_ways"):
        log.info(f"[{trip['id']}] OW out: {trip['origin']}->{trip['destination']}")
        op={"type":"2","departure_id":trip["origin"],"arrival_id":trip["destination"],
            "outbound_date":trip["outbound_date"],"adults":str(trip["passengers"]),"stops":trip.get("stops","0"),"travel_class":trip.get("cabin","1")}
        if trip.get("outbound_airline"):op["include_airlines"]=trip["outbound_airline"]
        ow_out=serpapi_search(op);results["api_calls"]+=1
        log.info(f"[{trip['id']}] OW ret: {trip['destination']}->{trip['origin']}")
        rp={"type":"2","departure_id":trip["destination"],"arrival_id":trip["origin"],
            "outbound_date":trip["return_date"],"adults":str(trip["passengers"]),"stops":trip.get("stops","0"),"travel_class":trip.get("cabin","1")}
        if trip.get("return_airline"):rp["include_airlines"]=trip["return_airline"]
        ow_ret=serpapi_search(rp);results["api_calls"]+=1
        ol=find_lowest(ow_out["flights"]);rl=find_lowest(ow_ret["flights"])
        if ol and rl:
            results["ow_combined"]={"lowest_total":ol["price"]+rl["price"],"out_price":ol["price"],"out_airline":ol.get("airline",""),"out_duration":ol.get("duration"),"ret_price":rl["price"],"ret_airline":rl.get("airline",""),"ret_duration":rl.get("duration")}
        os2=find_shortest(ow_out["flights"]);rs2=find_shortest(ow_ret["flights"])
        if os2 and rs2:
            results["ow_combined_shortest"]={"total":os2["price"]+rs2["price"],"out_price":os2["price"],"out_airline":os2.get("airline",""),"ret_price":rs2["price"],"ret_airline":rs2.get("airline","")}
    return results

def validate(results):
    score,issues=100,[]
    if results.get("rt_status") not in ("Success","success"):score-=20;issues.append("API not success")
    if results.get("rt_num_results",0)==0:score-=50;issues.append("No flights")
    elif results.get("rt_num_results",0)<3:score-=10;issues.append(f"Only {results['rt_num_results']} results")
    lp=results.get("rt_lowest",{})
    if not lp or not lp.get("price"):score-=30;issues.append("No price")
    elif lp["price"]<50:score-=25;issues.append(f"Low: ${lp['price']}")
    grade="A" if score>=90 else "B" if score>=75 else "C" if score>=60 else "F"
    return {"score":max(0,score),"grade":grade,"issues":issues,"passed":score>=60}

def load_history():
    p=CONFIG["data_dir"]/"price_history.json"
    return json.load(open(p)) if p.exists() else {}

def save_history(h2):
    with open(CONFIG["data_dir"]/"price_history.json","w") as f:json.dump(h2,f,indent=2,default=str)

def add_to_history(history,tid,results):
    if tid not in history:history[tid]={"checks":[]}
    entry={"timestamp":results["timestamp"],
        "rt_lowest_price":results.get("rt_lowest",{}).get("price") if results.get("rt_lowest") else None,
        "rt_lowest_duration":results.get("rt_lowest",{}).get("duration") if results.get("rt_lowest") else None,
        "rt_lowest_airline":results.get("rt_lowest",{}).get("airline") if results.get("rt_lowest") else None,
        "rt_shortest_price":results.get("rt_shortest",{}).get("price") if results.get("rt_shortest") else None,
        "rt_shortest_duration":results.get("rt_shortest",{}).get("duration") if results.get("rt_shortest") else None,
        "ow_combined_lowest":results.get("ow_combined",{}).get("lowest_total") if results.get("ow_combined") else None,
        "ow_out_price":results.get("ow_combined",{}).get("out_price") if results.get("ow_combined") else None,
        "ow_out_airline":results.get("ow_combined",{}).get("out_airline") if results.get("ow_combined") else None,
        "ow_ret_price":results.get("ow_combined",{}).get("ret_price") if results.get("ow_combined") else None,
        "ow_ret_airline":results.get("ow_combined",{}).get("ret_airline") if results.get("ow_combined") else None,
        "price_level":results.get("price_insights",{}).get("price_level",""),
        "typical_range":results.get("price_insights",{}).get("typical_price_range",[]),
        "api_calls":results.get("api_calls",1)}
    history[tid]["checks"].append(entry);save_history(history);return entry

def get_stats(history,tid):
    checks=history.get(tid,{}).get("checks",[])
    rp=[c["rt_lowest_price"] for c in checks if c.get("rt_lowest_price")]
    if not rp:return None
    return {"count":len(checks),"rt_current":rp[-1],"rt_lowest":min(rp),"rt_highest":max(rp),"rt_avg":sum(rp)/len(rp),
        "rt_trend":"down" if len(rp)>=2 and rp[-1]<rp[-2] else "up" if len(rp)>=2 and rp[-1]>rp[-2] else "flat"}

def track_api_usage(calls):
    p=CONFIG["data_dir"]/"api_usage.json"
    usage=json.load(open(p)) if p.exists() else {}
    m=datetime.utcnow().strftime("%Y-%m");usage[m]=usage.get(m,0)+calls
    with open(p,"w") as f:json.dump(usage,f,indent=2)
    return usage[m]

def fmt_dur(mins):
    if not mins:return "?"
    return f"{mins//60}h {mins%60}m"

def send_discord(trip,results,stats,val):
    url=CONFIG.get("discord_webhook_url")
    if not url:return
    rt_lp=results.get("rt_lowest",{}).get("price") if results.get("rt_lowest") else None
    rt_sp=results.get("rt_shortest",{}).get("price") if results.get("rt_shortest") else None
    rt_sd=results.get("rt_shortest",{}).get("duration") if results.get("rt_shortest") else None
    ow=results.get("ow_combined");pi=results.get("price_insights",{})
    booked=trip.get("booked_price");prices=[p for p in [rt_lp,rt_sp,ow.get("lowest_total") if ow else None] if p]
    best=min(prices) if prices else rt_lp;drop=booked-best if booked and best else None
    if drop and drop>=trip.get("alert_threshold",100):color,pfx=0x10B981,"🟢 PRICE DROP"
    elif drop and drop>0:color,pfx=0xF59E0B,"🟡 Small drop"
    elif drop and drop<0:color,pfx=0xEF4444,"🔴 Price up"
    else:color,pfx=0x3B82F6,"✈️ Price check"
    title=f"{pfx} — {trip['origin']} → {trip['destination']}"
    fields=[{"name":"🔄 ROUND TRIP","value":"─────────────","inline":False}]
    if rt_lp:fields.append({"name":"Lowest","value":f"**${rt_lp}**","inline":True})
    if rt_sp:fields.append({"name":"Shortest","value":f"**${rt_sp}** ({fmt_dur(rt_sd)})","inline":True})
    if booked and rt_lp:d2=booked-rt_lp;fields.append({"name":"vs Booked","value":f"{'↓' if d2>0 else '↑'} **${abs(d2):.0f}**","inline":True})
    if ow:
        fields.append({"name":"🔀 TWO ONE-WAYS","value":"─────────────","inline":False})
        fields.append({"name":"Outbound","value":f"${ow['out_price']} ({ow.get('out_airline','?')})","inline":True})
        fields.append({"name":"Return","value":f"${ow['ret_price']} ({ow.get('ret_airline','?')})","inline":True})
        fields.append({"name":"Combined","value":f"**${ow['lowest_total']}**","inline":True})
        if rt_lp:diff=rt_lp-ow["lowest_total"];fields.append({"name":"💡","value":f"OW saves **${diff:.0f}**" if diff>0 else f"RT saves ${abs(diff):.0f}","inline":False}) if diff!=0 else None
    fields=[f for f in fields if f]
    typical=pi.get("typical_price_range",[])
    if typical:fields.append({"name":"Typical","value":f"${typical[0]}–${typical[1]} ({pi.get('price_level','?')})","inline":True})
    if stats:ti="📉" if stats["rt_trend"]=="down" else "📈" if stats["rt_trend"]=="up" else "➡️";fields.append({"name":"Trend","value":f"{ti} {stats['rt_trend']}","inline":True})
    fields.append({"name":"Grade","value":f"{val['grade']} ({val['score']}/100)","inline":True})
    mu=track_api_usage(0);fields.append({"name":"API","value":f"{mu}/250","inline":True})
    cabin_label={"1":"Economy","2":"Premium Econ","3":"Business","4":"First"}.get(trip.get("cabin","1"),"Economy")
    embed={"title":title,"color":color,"fields":fields,
        "footer":{"text":f"{trip.get('name','')} · {cabin_label} · {trip['outbound_date']} → {trip.get('return_date','')} · {trip['passengers']}pax · {results.get('api_calls',1)} calls"},
        "timestamp":results["timestamp"]}
    payload={"username":"Flight Monitor","avatar_url":"https://em-content.zobj.net/source/apple/391/airplane_2708-fe0f.png","embeds":[embed]}
    try:
        resp=requests.post(url,json=payload,timeout=10)
        if resp.status_code in (200,204):log.info(f"[{trip['id']}] Discord sent")
        else:log.error(f"[{trip['id']}] Discord {resp.status_code}")
    except Exception as e:log.error(f"[{trip['id']}] Discord fail: {e}")

def process_trip(trip,history):
    log.info(f"[{trip['id']}] === {trip['origin']}->{trip['destination']} ({trip['name']}) ===")
    try:results=check_trip_prices(trip)
    except Exception as e:log.error(f"[{trip['id']}] API error: {e}");return {"trip_id":trip["id"],"error":str(e),"api_calls":0}
    val=validate(results);log.info(f"[{trip['id']}] Grade: {val['grade']} | Calls: {results['api_calls']}")
    mu=track_api_usage(results["api_calls"]);log.info(f"[{trip['id']}] API month: {mu}/250")
    add_to_history(history,trip["id"],results);stats=get_stats(history,trip["id"])
    rt_lp=results.get("rt_lowest",{}).get("price") if results.get("rt_lowest") else None
    ow_lp=results.get("ow_combined",{}).get("lowest_total") if results.get("ow_combined") else None
    best=min([p for p in [rt_lp,ow_lp] if p]) if any([rt_lp,ow_lp]) else None
    drop=trip["booked_price"]-best if trip.get("booked_price") and best else None
    log.info(f"[{trip['id']}] RT: ${rt_lp or '?'} | Short: ${results.get('rt_shortest',{}).get('price','?') if results.get('rt_shortest') else '?'}")
    always=os.environ.get("ALWAYS_NOTIFY","true").lower()=="true"
    threshold_met=drop is not None and drop>=trip.get("alert_threshold",100)
    if always or threshold_met:send_discord(trip,results,stats,val)
    return {"trip_id":trip["id"],"rt_lowest":rt_lp,"rt_shortest":results.get("rt_shortest",{}).get("price") if results.get("rt_shortest") else None,
        "ow_combined":ow_lp,"best_price":best,"drop":drop,"alert":threshold_met,"grade":val["grade"],"api_calls":results["api_calls"]}

def run_all():
    if not CONFIG["api_key"]:log.error("SERPAPI_KEY not set");sys.exit(1)
    active=[t for t in TRIPS if t.get("active",True)]
    log.info(f"Checking {len(active)} trip(s)");history=load_history();results=[];total=0
    for trip in active:
        if trip.get("outbound_date") and trip["outbound_date"]<date.today().isoformat():
            log.info(f"[{trip['id']}] Skip past");continue
        r=process_trip(trip,history);results.append(r);total+=r.get("api_calls",0)
    log.info("="*55);log.info(f"DONE — {total} API calls")
    for r in results:
        s="!!ALERT" if r.get("alert") else "ok"
        log.info(f"  {s} {r['trip_id']}: RT=${r.get('rt_lowest','?')} Short=${r.get('rt_shortest','?')} OW=${r.get('ow_combined','N/A')}")
    log.info("="*55);return results

def show_status():
    history=load_history()
    if not history:print("No history.");return
    print(f"\n{'='*55}\n  Flight Monitor v3.1\n{'='*55}")
    for trip in TRIPS:
        stats=get_stats(history,trip["id"])
        if not stats:print(f"\n  {trip['origin']}->{trip['destination']} ({trip['name']}) — no data");continue
        d=(date.fromisoformat(trip["outbound_date"])-date.today()).days
        print(f"\n  {trip['origin']}->{trip['destination']} ({trip['name']}) — {d}d")
        print(f"    Checks:{stats['count']} Cur:${stats['rt_current']} Low:${stats['rt_lowest']} Avg:${stats['rt_avg']:.0f} {stats['rt_trend']}")
    p=CONFIG["data_dir"]/"api_usage.json"
    if p.exists():u=json.load(open(p));m=datetime.utcnow().strftime("%Y-%m");print(f"\n  API ({m}): {u.get(m,0)}/250")
    print(f"\n{'='*55}\n")

if __name__=="__main__":
    import argparse;parser=argparse.ArgumentParser();parser.add_argument("command",choices=["check","status","test"])
    args=parser.parse_args()
    if args.command=="check":r=run_all();print(json.dumps(r,indent=2,default=str))
    elif args.command=="status":show_status()
    elif args.command=="test":
        if not CONFIG["discord_webhook_url"]:print("No DISCORD_WEBHOOK");sys.exit(1)
        tr={"timestamp":datetime.utcnow().isoformat()+"Z","trip_id":"test","api_calls":3,
            "rt_status":"Success","rt_num_results":12,
            "rt_lowest":{"price":612,"duration":490,"airline":"Delta"},
            "rt_shortest":{"price":689,"duration":370,"airline":"Delta"},
            "ow_enabled":True,"ow_combined":{"lowest_total":567,"out_price":289,"out_airline":"Delta","ret_price":278,"ret_airline":"United"},
            "ow_combined_shortest":None,"price_insights":{"price_level":"low","typical_price_range":[550,920]}}
        tt={"id":"test","name":"Test","origin":"CVG","destination":"OGG","outbound_date":"2026-11-21","return_date":"2026-11-28",
            "passengers":2,"booked_price":750,"alert_threshold":100,"cabin":"1"}
        send_discord(tt,tr,{"count":5,"rt_current":612,"rt_lowest":580,"rt_highest":750,"rt_avg":665,"rt_trend":"down"},
            {"score":92,"grade":"A","issues":[],"passed":True})
        print("Test sent.")
