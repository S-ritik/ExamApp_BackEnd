from accounts.utils import get_current_db

class InstituteRouter:
    """
    A router to control all database operations on models for multi-tenancy.
    """
    
    # Models that should always stay in the 'default' (shared) database
    SHARED_MODELS = {
        # userinstitutemembership links two shared models, so it is shared too;
        # its center FK points into the tenant DB (db_constraint=False).
        'accounts': ['institute', 'user', 'userinstitutemembership', 'devicesession', 'activitylog'],
        'contenttypes': None, # All models in this app
        'auth': None,        # All models in this app
        'sessions': None,    # All models in this app
        'admin': None,       # All models in this app
    }

    def db_for_read(self, model, **hints):
        """
        Attempts to read models go to the tenant database unless they are shared.
        """
        app_label = model._meta.app_label
        model_name = model._meta.model_name
        
        if app_label in self.SHARED_MODELS:
            shared_models = self.SHARED_MODELS[app_label]
            if shared_models is None or model_name in shared_models:
                return 'default'
        
        return get_current_db()

    def db_for_write(self, model, **hints):
        """
        Attempts to write models go to the tenant database unless they are shared.
        """
        app_label = model._meta.app_label
        model_name = model._meta.model_name
        
        if app_label in self.SHARED_MODELS:
            shared_models = self.SHARED_MODELS[app_label]
            if shared_models is None or model_name in shared_models:
                return 'default'
        
        return get_current_db()

    def allow_relation(self, obj1, obj2, **hints):
        """
        Allow all relations including cross-db ones.
        This system uses db_constraint=False for cross-db FKs so Django
        handles them in application code rather than at DB level.
        """
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """
        Ensure shared models only migrate to 'default'.
        Tenant models should migrate to both if we want them available.
        """
        if app_label in self.SHARED_MODELS:
            shared_models = self.SHARED_MODELS[app_label]
            if shared_models is None or model_name in shared_models:
                return db == 'default'
        
        # If it's not a shared model, it can be migrated to tenant DBs
        # (Assuming we manage tenant migrations carefully)
        return True
