import pandas as pd
import os
from datetime import datetime

today = datetime.today().strftime("%Y-%m-%d")
inventory_path = f"/tmp/vs-inventory.csv"
updated_xlsx = f"/K8S-Inventory/Updated/vs-inventory-merged-{today}.xlsx"

# 1. Load current inventory
df_new = pd.read_csv(inventory_path, header=None)
df_new.columns = ["Namespace", "VirtualService", "Hosts", "Gateways", "ContextPath"]
df_new["ScanKey"] = df_new["Hosts"] + df_new["ContextPath"]

# 2. Find latest scanned file
dir_antt = "/K8S-Inventory/ANTT"
files = sorted([f for f in os.listdir(dir_antt) if f.startswith("k8s-vs-inventory") and f.endswith(".xlsx")])
if not files:
    raise Exception("Không tìm thấy file nào trong thư mục ANTT")

latest_file = os.path.join(dir_antt, files[-1])
df_old = pd.read_excel(latest_file)
df_old["ScanKey"] = df_old["Hosts"] + df_old["ContextPath"]

# 3. Merge Status
df_merged = pd.merge(
    df_new,
    df_old[["ScanKey", "Status"]],
    how="left",
    on="ScanKey"
)
df_merged["Status"] = df_merged["Status"].fillna("🆕 NEW")

# 4. Ghi kết quả
df_merged.drop(columns=["ScanKey"]).to_excel(updated_xlsx, index=False)
print(f"[+] Wrote updated file: {updated_xlsx}")