"""Mock SAP procurement connector (local, free, no SAP licence).

It mimics the shape of SAP S/4HANA procurement entities so a real OData adapter can be dropped in later:
  Supplier            (from the ERP seed's suppliers)
  Material            (from the ERP seed's catalog items, plus engine maintenance spares)
  PurchaseRequisition PurchaseRequisition, items[PurchaseRequisitionItem, Material, RequestedQuantity, BaseUnit, Plant,
                      DeliveryDate, PurchasingGroup, ValuationPrice, Currency], Status, CreatedByUser, Reason, Source
  PurchaseOrder       PurchaseOrder, Supplier, items[...], Status, NetAmount, Currency

Reads are free for agents. Creating a requisition or converting it to a purchase order is a WRITE tool: the agent runtime
queues it for a named human approver, and only the approved call reaches this connector.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from typing import Any

from ..config import DATA

# engine maintenance spares (illustrative catalogue, prices are demo values)
SPARES = [
    {"Material": "BSK-ENG-KIT", "MaterialName": "Borescope inspection consumables kit", "BaseUnit": "EA", "Price": 420.0, "Supplier": "100004"},
    {"Material": "HPC-BLD-SET", "MaterialName": "HPC stage blade set (overhaul exchange)", "BaseUnit": "SET", "Price": 18500.0, "Supplier": "100004"},
    {"Material": "FAN-BLD-01", "MaterialName": "Fan blade, single", "BaseUnit": "EA", "Price": 6200.0, "Supplier": "100004"},
    {"Material": "SEAL-KIT-HPT", "MaterialName": "HPT seal kit", "BaseUnit": "KIT", "Price": 1350.0, "Supplier": "100004"},
    {"Material": "T50-PROBE", "MaterialName": "EGT (T50) thermocouple probe", "BaseUnit": "EA", "Price": 780.0, "Supplier": "100004"},
]


class MockSAP:
    """Thread-safe in-memory SAP procurement. One instance per agent runtime (live and each sandbox)."""

    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        seed = seed or json.loads((DATA / "erp_seed.json").read_text())
        self.lock = threading.Lock()
        self.suppliers: dict[str, dict[str, Any]] = {}
        for i, s in enumerate(seed.get("suppliers", []), start=1):
            self.suppliers[f"1000{i:02d}"] = {"Supplier": f"1000{i:02d}", "SupplierName": s["name"], "LegacyId": s["id"],
                                              "Materials": s.get("materials", []), "LeadTimeDays": s.get("lead_days"),
                                              "OnTimeRate": s.get("on_time")}
        self.suppliers.setdefault("100004", {"Supplier": "100004", "SupplierName": "Aero Engine Spares (demo)", "LegacyId": "-",
                                             "Materials": [x["Material"] for x in SPARES], "LeadTimeDays": 7, "OnTimeRate": 0.96})
        self.materials: dict[str, dict[str, Any]] = {x["Material"]: dict(x) for x in SPARES}
        for it in seed.get("items", []):
            self.materials[it["code"]] = {"Material": it["code"], "MaterialName": it["description"], "BaseUnit": "KG",
                                          "Price": it.get("rate_per_kg", 0.0), "Supplier": None}
        legacy = {s["LegacyId"]: k for k, s in self.suppliers.items()}
        self.purchase_orders: dict[str, dict[str, Any]] = {}
        for n, m in enumerate(seed.get("material_pos", [])):
            po = f"45000{n:05d}"
            self.purchase_orders[po] = {"PurchaseOrder": po, "Supplier": legacy.get(m["supplier"], "100001"),
                                        "Status": "open", "Currency": "CAD", "LegacyId": m["po"],
                                        "items": [{"PurchaseOrderItem": "10", "Material": m["material"], "OrderQuantity": m["kg"],
                                                   "BaseUnit": "KG", "DeliveryDate": m["due"]}]}
        self.requisitions: dict[str, dict[str, Any]] = {}
        self._pr_seq = 10000000
        self._po_seq = 4500100000

    # ---- reads -------------------------------------------------------------------------------
    def list_suppliers(self) -> list[dict[str, Any]]:
        return list(self.suppliers.values())

    def list_materials(self, query: str = "") -> list[dict[str, Any]]:
        q = query.lower()
        return [m for m in self.materials.values() if not q or q in m["Material"].lower() or q in m["MaterialName"].lower()][:50]

    def list_purchase_orders(self, status: str = "") -> list[dict[str, Any]]:
        return [p for p in self.purchase_orders.values() if not status or p["Status"] == status]

    def list_requisitions(self, status: str = "") -> list[dict[str, Any]]:
        return sorted((r for r in self.requisitions.values() if not status or r["Status"] == status),
                      key=lambda r: r["PurchaseRequisition"], reverse=True)

    def summary(self) -> dict[str, Any]:
        return {"suppliers": len(self.suppliers), "materials": len(self.materials), "purchase_orders": len(self.purchase_orders),
                "requisitions": len(self.requisitions),
                "requisition_value": round(sum(r["NetAmount"] for r in self.requisitions.values()), 2)}

    # ---- writes (only ever reached through an approved tool call) ---------------------------
    def create_purchase_requisition(self, material: str, quantity: float, reason: str, plant: str = "CA01",
                                    delivery_days: int = 14, source: str = "", requested_by: str = "agent") -> dict[str, Any]:
        m = self.materials.get(material)
        if m is None:
            raise ValueError(f"unknown material {material}")
        if not 0 < float(quantity) <= 10000:
            raise ValueError("quantity must be between 0 and 10000")
        with self.lock:
            self._pr_seq += 1
            pr = str(self._pr_seq)
            net = round(float(quantity) * float(m["Price"]), 2)
            rec = {"PurchaseRequisition": pr, "Status": "created", "CreatedByUser": requested_by, "Reason": reason[:300],
                   "Source": source[:80], "CreationDate": dt.date.today().isoformat(), "Currency": "CAD", "NetAmount": net,
                   "items": [{"PurchaseRequisitionItem": "10", "Material": material, "MaterialName": m["MaterialName"],
                              "RequestedQuantity": float(quantity), "BaseUnit": m["BaseUnit"], "Plant": plant,
                              "DeliveryDate": (dt.date.today() + dt.timedelta(days=int(delivery_days))).isoformat(),
                              "PurchasingGroup": "P01", "ValuationPrice": m["Price"], "FixedSupplier": m.get("Supplier")}]}
            self.requisitions[pr] = rec
        return rec

    def convert_to_purchase_order(self, requisition: str) -> dict[str, Any]:
        with self.lock:
            r = self.requisitions.get(requisition)
            if r is None:
                raise ValueError(f"unknown purchase requisition {requisition}")
            if r["Status"] != "created":
                raise ValueError(f"requisition {requisition} is {r['Status']}")
            self._po_seq += 1
            po = str(self._po_seq)
            it = r["items"][0]
            self.purchase_orders[po] = {"PurchaseOrder": po, "Supplier": it.get("FixedSupplier") or "100004", "Status": "released",
                                        "Currency": r["Currency"], "NetAmount": r["NetAmount"], "FromRequisition": requisition,
                                        "items": [{"PurchaseOrderItem": "10", "Material": it["Material"],
                                                   "OrderQuantity": it["RequestedQuantity"], "BaseUnit": it["BaseUnit"],
                                                   "DeliveryDate": it["DeliveryDate"]}]}
            r["Status"] = "converted"
            r["PurchaseOrder"] = po
        return self.purchase_orders[po]


def register(runtime: Any) -> None:
    """Add the SAP tools to an agent runtime (risk tiers decide what needs approval)."""
    from ..agents import Risk, Tool

    sap = MockSAP()
    runtime.sap = sap
    runtime.register(Tool("sap.list_suppliers", Risk.READ, {}, sap.list_suppliers, "SAP suppliers (mock)"))
    runtime.register(Tool("sap.list_materials", Risk.READ, {"query": "str?"}, sap.list_materials, "SAP material master (mock)"))
    runtime.register(Tool("sap.list_purchase_orders", Risk.READ, {"status": "str?"}, sap.list_purchase_orders,
                          "SAP purchase orders (mock)"))
    runtime.register(Tool("sap.create_purchase_requisition", Risk.WRITE,
                          {"material": "str", "quantity": "float", "reason": "str", "plant": "str?", "delivery_days": "int?",
                           "source": "str?", "requested_by": "str?"},
                          sap.create_purchase_requisition, "Create an SAP purchase requisition (approval required)"))
    runtime.register(Tool("sap.convert_to_purchase_order", Risk.WRITE, {"requisition": "str"}, sap.convert_to_purchase_order,
                          "Convert an approved requisition into a released purchase order (approval required)"))
