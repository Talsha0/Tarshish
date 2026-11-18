"""Application services shared by the CLI's front ends (the Streamlit dashboard and the admin API).

Unlike ``core``, which must not know any concrete plugin, a service may combine the engine with the
publisher and generator plugins it drives.
"""
