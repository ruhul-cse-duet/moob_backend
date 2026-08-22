"""Every path in the Flutter `Api` class must exist on the server.

The app names its endpoints in one file; FastAPI publishes its own route table.
Comparing the two catches a rename on either side before a screen 404s.
"""
import json, re, sys, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8002"
DART = r"c:\Ahsan\app\mmm\moob02\lib\service\api\api_endpoints.dart"

spec = json.load(urllib.request.urlopen(f"{BASE}/api/v1/openapi.json"))
server = {re.sub(r"\{[^}]+\}", "{}", p.replace("/api/v1", "", 1)) for p in spec["paths"]}

src = open(DART, encoding="utf-8").read()
app_paths = {}
for name, path in re.findall(r"static const String (\w+) = '([^']+)'", src):
    app_paths[name] = path
for name, body in re.findall(r"static String (\w+)\([^)]*\) =>\s*'([^']+)'", src):
    app_paths[name] = re.sub(r"\$\{?\w+\}?", "{}", body)

missing = {n: p for n, p in app_paths.items() if p not in server}
print(f"{len(app_paths)} app paths checked against {len(server)} server routes")
if missing:
    print(f"\n{len(missing)} path(s) the server does not serve:")
    for n, p in sorted(missing.items(), key=lambda kv: kv[1]):
        print(f"  {p:52} (Api.{n})")
    sys.exit(1)
print("every path the app calls exists on the server")
