import os, sys, asyncio, json, time, cProfile, pstats
sys.path.append('C:/Users/n0souls/gms2-mcp-server/mcp-serv')
os.environ['GMS2_PROJECT_PATH'] = 'C:/Users/n0souls/Documents/GitHub/Undefinedtale-888/Undefinedtale888'

from server import get_gml_definitions_index, scan_project

async def run_calls():
    # Warm up (Cold)
    await get_gml_definitions_index()
    await scan_project()
    
    # Measured (Hot)
    print("\n--- HOT CALL: get_gml_definitions_index ---")
    t0 = time.time()
    await get_gml_definitions_index()
    print(f"Duration: {time.time()-t0:.6f}s")
    
    print("\n--- HOT CALL: scan_project ---")
    t0 = time.time()
    await scan_project()
    print(f"Duration: {time.time()-t0:.6f}s")

def profile_it():
    profiler = cProfile.Profile()
    profiler.enable()
    asyncio.run(run_calls())
    profiler.disable()
    stats = pstats.Stats(profiler).sort_stats('cumulative')
    stats.print_stats(30) # Top 30

if __name__ == "__main__":
    profile_it()
