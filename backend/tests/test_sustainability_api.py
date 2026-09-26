"""Sustainability wired into /api/evaluate, saved evaluations, suggestions and closet items (offline, no Gemini)."""
import json

from app.sustainability import score_item


def _ev(client, item_id, price):
    r = client.post("/api/evaluate", json={"item_id": item_id, "price": price})
    assert r.status_code == 200, r.text
    return r.json()


def test_evaluate_includes_sustainability(client, candidates):
    res = _ev(client, candidates["bottom_womens"], 45)
    s = res["sustainability"]
    assert s["supported"] is True and s["is_estimate"] is True
    assert 0 <= s["score"] <= 100 and s["grade"] in "ABCDE" and len(s["reasons"]) == 2
    assert s["garment_type"] == "trousers" and s["cost_per_wear"] is not None
    # same inputs straight into the module -> same numbers (n, top similarity, price, the app's thresholds)
    settings = client.get("/api/settings").json()
    direct = score_item(res["item"]["attributes"], res["item"]["category"], res["total_new_outfits"],
                        res["redundancy"]["top_similarity"], 45,
                        dup_threshold=settings["redundancy_duplicate_threshold"],
                        similar_threshold=settings["redundancy_similar_threshold"])
    assert (direct["score"], direct["expected_wears"]) == (s["score"], s["expected_wears"])
    assert s["components"]["redundancy_level"] in (res["redundancy"]["level"], None)


def test_sustainability_never_changes_the_verdict(client, candidates, monkeypatch):
    from app import evaluate
    normal = _ev(client, candidates["near_dup_top"], 28)

    def boom(*a, **k):
        raise RuntimeError("estimator broken")
    monkeypatch.setattr(evaluate, "score_item", boom)
    broken = _ev(client, candidates["near_dup_top"], 28)
    assert broken["sustainability"] is None
    assert broken["verdict"] == normal["verdict"] and broken["value"] == normal["value"]
    assert normal["verdict"]["decision"] == "SKIP" and normal["sustainability"]["components"]["redundancy_level"] == "near_duplicate"


def test_accessory_sustainability_unsupported(client):
    from app import db
    iid = db.insert_item(status="detected", category="accessory", source="test",
                         attributes={"category": "accessory", "subcategory": "handbag"})
    res = _ev(client, iid, 30)
    assert res["verdict"]["decision"] == "UNSUPPORTED"
    assert res["sustainability"] is None  # no estimate for accessories (the UI hides the card)
    db.delete_item(iid)


def test_saved_evaluation_computed_on_read(client, candidates):
    from app import db
    res = _ev(client, candidates["bottom_womens"], 45)
    eid = res["evaluation_id"]
    with db.get_conn() as c:  # simulate an evaluation saved before the score existed
        row = json.loads(c.execute("SELECT results FROM evaluations WHERE id=?", (eid,)).fetchone()[0])
        row.pop("sustainability")
        c.execute("UPDATE evaluations SET results=? WHERE id=?", (json.dumps(row), eid))
    got = client.get(f"/api/evaluations/{eid}").json()
    assert got["evaluation_id"] == eid and "settings" not in got
    assert got["sustainability"]["score"] == res["sustainability"]["score"]
    assert got["verdict"] == res["verdict"]
    assert client.get("/api/evaluations/ev_nope").status_code == 404


def test_item_sustainability_endpoint(client, candidates):
    from app import db
    res = _ev(client, candidates["bottom_womens"], 45)
    r = client.get(f"/api/items/{candidates['bottom_womens']}/sustainability")
    assert r.status_code == 200 and r.json()["score"] == res["sustainability"]["score"]
    assert r.json()["evaluation_id"] == res["evaluation_id"]
    never = next(i for i in db.list_items(status="closet")
                 if db.latest_evaluation_for_item(i["id"]) is None)
    assert client.get(f"/api/items/{never['id']}/sustainability").status_code == 404


def test_cached_suggestions_get_sustainability_on_read(client, candidates):
    from app import db
    res = _ev(client, candidates["bottom_womens"], 45)
    eid = res["evaluation_id"]
    item = db.get_item(candidates["near_dup_top"])  # any real item stands in for a stored suggestion
    old = {"evaluation_id": eid, "mode": "pairings", "title": "Pairs well with this", "suggestions": [{
        "id": "it_old", "name": "Striped Boatneck Sweater in Everyday Cotton", "price": 59.0, "category": "top",
        "item": {**item, "attributes": {**item["attributes"], "subcategory": "t-shirt", "fabric_guess": "cotton"}},
        "total_new_outfits": 39, "redundancy": {"level": "none", "top_similarity": 0.7}}]}
    db.save_suggestions(eid, "pairings", old)
    s = client.get(f"/api/evaluations/{eid}/suggestions").json()["suggestions"][0]["sustainability"]
    assert s["supported"] and s["garment_type"] == "sweater"  # the product title beats the planned subcategory
    assert s["materials"][0]["material"] == "cotton"
    s2 = client.post(f"/api/evaluations/{eid}/suggestions").json()["suggestions"][0]["sustainability"]
    assert s2["score"] == s["score"]


def test_item_sustainability_uses_current_attributes(client, candidates):
    iid = candidates["dress_womens"]
    _ev(client, iid, 22)
    before = client.get(f"/api/items/{iid}/sustainability").json()
    r = client.patch(f"/api/closet/items/{iid}", json={"attributes": {"fabric_guess": "linen"}})
    assert r.status_code == 200, r.text
    after = client.get(f"/api/items/{iid}/sustainability").json()
    assert after["materials"][0]["material"] == "linen"
    assert after["expected_wears"] == before["expected_wears"]  # stats from the stored evaluation
    assert after["footprint_kg_co2e"] < before["footprint_kg_co2e"]  # linen 15 vs average textile 21.6 kg/kg
