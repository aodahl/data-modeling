import asyncio

import httpx

from app.main import app, lifespan


def test_startup_health_page_and_workflow():
    async def workflow():
      async with lifespan(app):
       async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        health=await client.get("/health")
        assert health.status_code==200
        assert health.json()["operational_patients"]==10
        page=await client.get("/")
        assert page.status_code==200
        assert "Operational ↔ Analytical" in page.text
        rebuilt=await client.post("/model/build",data={"grain_id":"claim_line","style":"snowflake"},follow_redirects=True)
        assert rebuilt.status_code==200
        assert "Snowflake analytical model" in rebuilt.text
        ran=await client.post("/questions/run",data={"question_id":"events_by_year"},follow_redirects=True)
        assert ran.status_code==200
        assert "EXPLAIN QUERY PLAN" in ran.text
        simulated=await client.post("/scd2/simulate",follow_redirects=True)
        assert simulated.status_code==200
        assert "Toronto" in simulated.text
        rejected=await client.post("/query/run",data={"target":"operational","sql":"DROP TABLE patient"},follow_redirects=True)
        assert "Only SELECT queries" in rejected.text
    asyncio.run(workflow())
