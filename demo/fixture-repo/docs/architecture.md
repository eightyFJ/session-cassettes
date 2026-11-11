# Architecture

Three small services run side by side: authentication, the product catalog, and a health probe.
Each one binds its own local port. See the service modules for details.
