import json
import os
import time
import requests
from google.cloud import compute_v1

# ==========================================
# 1. LOAD CONFIGURATIONS & CREDENTIALS
# ==========================================
with open("var.config", "r") as f:
    config = json.load(f)

with open("cred.config", "r") as f:
    cred = json.load(f)

# Explicitly set GCP credentials so the script never fails authentication
cred_path = cred.get("google_application_credentials")
if cred_path:
    if os.path.exists(cred_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path
        print(f"✅ Loaded GCP credentials from: {cred_path}")
    else:
        print(f"❌ Error: Credential file not found at {cred_path}")

PROJECT_ID = config["project_id"]
BACKEND_SERVICE_NAME = config["backend_service_name"]
EXPECTED_SERVERS = set(config["expected_servers"])
WEBHOOK_URL = config["webhook_url"]

STATE_FILE = "downtime_state.json"

# ==========================================
# 2. STATE MANAGEMENT FUNCTIONS
# ==========================================
def load_state():
    """Loads the JSON file tracking which servers are down and since when."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    """Saves the downtime timestamps to a JSON file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def send_alert(message):
    """Fires the HTTP POST payload to Webhook.site / Slack."""
    payload = {"text": message}
    try:
        response = requests.post(WEBHOOK_URL, json=payload)
        print(f"   📡 Webhook triggered! Status Code: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Failed to send webhook notification: {e}")

# ==========================================
# 3. CORE MONITORING LOGIC
# ==========================================
def check_alb_health():
    client = compute_v1.BackendServicesClient()

    try:
        print(f"🔍 Querying Load Balancer: {BACKEND_SERVICE_NAME}...")
        
        # Fetch the ALB backend service configuration
        backend_service = client.get(
            project=PROJECT_ID, backend_service=BACKEND_SERVICE_NAME
        )

        healthy_servers = set()

        # Iterate through instance groups attached to the load balancer
        if backend_service.backends:
            for backend in backend_service.backends:
                ig_url = backend.group
                ig_name = ig_url.split("/")[-1]

                # Dynamically extract the zone from the URL
                parts = ig_url.split("/")
                if "zones" in parts:
                    ig_zone = parts[parts.index("zones") + 1]

                    # Query the Instance Group to see which VMs are actually inside and running
                    ig_client = compute_v1.InstanceGroupsClient()
                    list_request = compute_v1.ListInstancesInstanceGroupsRequest(
                        project=PROJECT_ID, 
                        zone=ig_zone, 
                        instance_group=ig_name
                    )

                    instance_response = ig_client.list_instances(request=list_request)

                    # Mark servers as healthy ONLY if they are in the group AND running
                    if instance_response.items:
                        for item in instance_response.items:
                            vm_name = item.instance.split("/")[-1]
                            if item.status == "RUNNING":
                                healthy_servers.add(vm_name)

        # Calculate which servers are missing (either stopped, or removed from the group)
        missing_servers = EXPECTED_SERVERS - healthy_servers
        current_time = time.time()
        state = load_state()

        # Process the missing servers to calculate downtime
        for server in EXPECTED_SERVERS:
            if server in missing_servers:
                if server not in state:
                    # First time this server is detected as down
                    state[server] = current_time

                # Calculate exact minutes down dynamically
                downtime_seconds = current_time - state[server]
                downtime_minutes = int(downtime_seconds // 60)

                msg = f"Alert: {server} is out of {BACKEND_SERVICE_NAME} since {downtime_minutes} minutes"
                print(f"   ⚠️  {msg}")
                
                # Send the webhook (it will fire every time the script runs while the server is down)
                send_alert(msg)
            
            else:
                # If the server is in the expected list, NOT missing, but is in the state file
                if server in state:
                    print(f"   ✅ [Resolved] {server} is back running in the ALB group.")
                    state.pop(server, None)

        # Save state for the next time the script runs
        save_state(state)
        print(f"📊 Status: {len(healthy_servers)}/3 servers are healthy in the ALB.")

    except Exception as e:
        print(f"❌ Error querying backend service health: {e}")

if __name__ == "__main__":
    print("🚀 Starting continuous ALB monitoring...")
    print("⏳ Checking every 60 seconds. Press [Ctrl + C] to stop.\n")
    
    try:
        while True:
            print("==========================================")
            check_alb_health()
            print("==========================================")
            
            # Wait for 60 seconds before checking again
            time.sleep(60) 
            
    except KeyboardInterrupt:
        # This catches the Ctrl+C keypress to exit gracefully
        print("\n🛑 Monitoring stopped by user. Exiting safely.")