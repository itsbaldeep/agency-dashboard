"""Browser-equivalent clients for authenticated dashboard route fixtures."""
def browser_client(app):
    client = app.test_client()
    client.environ_base['HTTP_ORIGIN'] = 'http://localhost'
    return client
