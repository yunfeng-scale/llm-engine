from ddtrace import tracer


def add_trace_resource_name(tag: str):
    """Adds a custom tag to a given dd trace corresponding to the route
    (e.g. get_model_bundles for GET /model-bundles, etc.) so that we can filter in Datadog easier
    """
    current_span = tracer.current_span()
    if current_span:
        current_span.set_tag("spellbook_serve.resource_name", tag)