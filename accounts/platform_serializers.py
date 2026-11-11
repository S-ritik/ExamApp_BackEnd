from rest_framework import serializers
from .models import Institute, User
from .serializers import SubdomainField

class PlatformInstituteSerializer(serializers.ModelSerializer):
    """
    Detailed institute view for Platform Owners, including super admin info.
    """
    super_admin = serializers.SerializerMethodField()
    user_count = serializers.IntegerField(source='get_user_count', read_only=True)
    active_user_count = serializers.IntegerField(source='get_active_user_count', read_only=True)
    center_count = serializers.IntegerField(source='centers.count', read_only=True)
    subdomain = SubdomainField(max_length=63)
    is_active = serializers.BooleanField(required=False)

    class Meta:
        model = Institute
        fields = [
            'id', 'name', 'subdomain', 'domain', 'description', 'address', 
            'contact_email', 'contact_phone', 'website', 'is_verified',
            'is_active', 'created_at', 'super_admin', 'user_count', 
            'active_user_count', 'center_count', 'db_name'
        ]
        read_only_fields = ['id', 'created_at', 'db_name', 'user_count', 'active_user_count', 'center_count']

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # The institute's existing status (Online or Paused) should remain unchanged
        # when its details are updated. The status should change only when the platform
        # owner explicitly includes is_active in the payload.
        if 'is_active' not in self.initial_data:
            attrs.pop('is_active', None)
        if 'is_verified' not in self.initial_data:
            attrs.pop('is_verified', None)
        return attrs

    def get_super_admin(self, obj):
        # find the creator or one of the super admins
        admin = User.objects.filter(institute=obj, role='super_admin').first()
        if admin:
            return {
                'id': admin.id,
                'full_name': admin.get_full_name(),
                'email': admin.email,
                'username': admin.username
            }
        return None

class CreateInstituteWithAdminSerializer(serializers.Serializer):
    """
    Atomic payload to create both an institute and its first super admin.
    """
    # Institute details
    name = serializers.CharField(max_length=255)
    subdomain = SubdomainField(max_length=63, help_text="Optional, e.g. 'iitmadras' for iitmadras.exams.dashoapp.com")
    domain = serializers.CharField(max_length=100, required=False, allow_blank=True)
    contact_email = serializers.EmailField()
    
    # Super Admin details
    admin_email = serializers.EmailField()
    admin_username = serializers.CharField(max_length=150)
    admin_first_name = serializers.CharField(max_length=150)
    admin_last_name = serializers.CharField(max_length=150)
    admin_password = serializers.CharField(write_only=True)

class PlatformUserSerializer(serializers.ModelSerializer):
    """
    User view for Platform Owners, including institute name.
    """
    institute_name = serializers.CharField(source='institute.name', read_only=True)
    full_name = serializers.CharField(source='get_full_name', read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'email', 'username', 'full_name', 'first_name', 'last_name', 
            'role', 'institute', 'institute_name', 'is_active', 'created_at'
        ]
