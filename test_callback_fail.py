import app
try:
    fig = app.update_stress_chart("Municipal Wards: Ahmedabad (48 Wards)", 0, False, "init")
    print("Success")
except Exception as e:
    import traceback
    traceback.print_exc()
