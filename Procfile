web: sh -c "export PYTHONPATH=$PYTHONPATH:/app && gunicorn akahu_to_budget.app:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT"
