"""Plugin management routes (Phase 8 #163).

Workspace-scoped plugin management:
- list accessible workspaces
- list registered plugins with per-workspace installation state
- inspect a plugin
- install a trusted/local plugin manifest (validated, identity-bound)
- enable / disable a workspace installation
- grant / revoke plugin capabilities explicitly

Authorization reuses the Phase 7 model: any workspace member may view;
only the workspace owner may install, enable, disable, or grant capabilities.
The backend is authoritative and never trusts the UI.
"""

from flask import current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Plugin, PluginInstallation, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE
from app.plugins import bp
from app.services.capabilities import Capability, CapabilityStore
from app.services.events import emit_event
from app.services.permissions import require_workspace_capability, resolve_workspace, role_for
from app.services.plugin_audit import record_plugin_audit
from app.services.plugin_catalog import (
    CatalogEntryNotFoundError,
    CatalogError,
    CatalogUnavailableError,
    load_plugin_catalog,
)
from app.services.plugin_compat import (
    IncompatibleCompatibilityError,
    InvalidCompatibilityError,
    compatibility_status,
    enforce_compatible,
)
from app.services.plugin_config import validate_plugin_config
from app.services.plugin_errors import list_workspace_error_reports
from app.services.plugin_ops import (
    PluginEntryPointMismatchError,
    PluginNotFoundError,
    PluginUpdateError,
    discover_plugins,
    update_plugin,
)
from app.services.plugin_signatures import (
    VERIFY_IF_PRESENT,
    PublisherSignatureError,
    enforce_publisher_trust,
)
from app.services.plugin_trust import (
    PluginTrustConfigError,
    trusted_publisher_ids,
)
from app.services.plugins import ManifestValidationError, PluginError, PluginManifest


class PluginAlreadyInstalledError(ValueError):
    """Raised when a plugin is already installed in the workspace."""


def _accessible_workspaces(user):
    """Return the workspaces ``user`` owns or is an active member of."""
    seen: dict[int, Workspace] = {}
    for ws in Workspace.query.filter_by(user_id=user.id).all():
        seen[ws.id] = ws
    memberships = WorkspaceMember.query.filter_by(user_id=user.id, status=STATUS_ACTIVE).all()
    for membership in memberships:
        ws = db.session.get(Workspace, membership.workspace_id)
        if ws is not None:
            seen[ws.id] = ws
    return sorted(seen.values(), key=lambda w: w.id)


def _installation(plugin_id: str, workspace_id: int) -> PluginInstallation | None:
    return PluginInstallation.query.filter_by(
        plugin_id=plugin_id,
        workspace_id=workspace_id,
    ).first()


def _serialize(plugin: Plugin, workspace_id: int) -> dict:
    """Serialize a plugin with its installation state for ``workspace_id``.

    ``config`` is deliberately not included: it may hold secrets and is not
    needed for the management surface.
    """
    installation = _installation(plugin.id, workspace_id)
    if installation is not None:
        granted = CapabilityStore.list_capabilities(plugin.id, workspace_id)
    else:
        granted = []
    status = compatibility_status(plugin.compatibility)
    return {
        "id": plugin.id,
        "name": plugin.name,
        "version": plugin.version,
        "description": plugin.description,
        "author": plugin.author,
        "entry_point": plugin.entry_point,
        "declared_capabilities": plugin.capabilities or [],
        "permissions": plugin.permissions or [],
        "dependencies": plugin.dependencies or [],
        "compatibility": plugin.compatibility or "any",
        "trust_state": plugin.trust_state,
        "trust_publisher": plugin.trust_publisher,
        "compatible_with_app": bool(status["compatible"]),
        "app_version": status["app_version"],
        "installed": installation is not None,
        "enabled": bool(installation is not None and installation.enabled),
        "granted_capabilities": granted,
    }


def _capability_name(value: str) -> str | None:
    """Return the Capability enum name for an enum name or value, else None."""
    for cap in Capability:
        if value in (cap.name, cap.value):
            return cap.name
    return None


def _upsert_plugin(manifest: PluginManifest) -> Plugin:
    """Create or reuse the global ``Plugin`` row for a validated manifest.

    The manifest id is the identity, and an existing plugin keeps its entry
    point: a conflicting entry point raises
    :class:`PluginEntryPointMismatchError` so an install/update can never
    retarget registered code.
    """
    plugin = Plugin.query.filter_by(id=manifest.id).first()
    if plugin is None:
        plugin = Plugin(
            id=manifest.id,
            name=manifest.name,
            version=manifest.version,
            description=manifest.description,
            author=manifest.author,
            entry_point=manifest.entry_point,
            capabilities=manifest.capabilities,
            permissions=manifest.permissions or [],
            dependencies=manifest.dependencies or [],
            compatibility=manifest.compatibility,
            configuration=manifest.configuration or {},
            trust_state=manifest.trust_state,
            trust_publisher=manifest.trust_publisher,
        )
        db.session.add(plugin)
        return plugin

    if plugin.entry_point != manifest.entry_point:
        raise PluginEntryPointMismatchError(
            "Manifest entry_point does not match the registered plugin."
        )

    # Trust metadata is derived from the verified signature, so refresh it even
    # when the global row already exists (for example after a key rotation).
    plugin.trust_state = manifest.trust_state
    plugin.trust_publisher = manifest.trust_publisher
    return plugin


def _install_in_workspace(manifest: PluginManifest, workspace_id: int) -> Plugin:
    """Register ``manifest`` and install it in ``workspace_id``.

    Shared by the local-manifest and catalog install routes so both paths keep
    the same identity binding and duplicate-install handling. Neither path
    creates capability grants. Returns the persisted plugin.

    Raises:
        PluginAlreadyInstalledError: If the plugin is already installed in the
            workspace (or a concurrent request won the unique constraint).
    """
    plugin = _upsert_plugin(manifest)
    if _installation(manifest.id, workspace_id) is not None:
        db.session.rollback()
        raise PluginAlreadyInstalledError

    installation = PluginInstallation(
        plugin_id=manifest.id,
        workspace_id=workspace_id,
        enabled=True,
        config=manifest.configuration or {},
        installed_by_id=current_user.id,
    )
    db.session.add(installation)
    try:
        db.session.commit()
    except Exception:  # uniqueness races map to a duplicate install
        db.session.rollback()
        raise PluginAlreadyInstalledError from None
    return plugin


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    """Plugin management page (workspace-scoped)."""
    workspaces = _accessible_workspaces(current_user)
    return render_template("plugins/index.html", workspaces=workspaces)


# --------------------------------------------------------------------------
# API: accessible workspaces
# --------------------------------------------------------------------------


@bp.route("/api/workspaces")
@login_required
def api_list_workspaces():
    workspaces = []
    for ws in _accessible_workspaces(current_user):
        workspaces.append(
            {
                "id": ws.id,
                "name": ws.name,
                "role": role_for(ws.id, current_user),
            }
        )
    return jsonify(workspaces)


# --------------------------------------------------------------------------
# API: list and inspect plugins (viewable by any workspace member)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins", methods=["GET"])
@login_required
def api_list_plugins(workspace_id: int):
    resolve_workspace(workspace_id)
    plugins = Plugin.query.order_by(Plugin.id).all()
    return jsonify(
        {
            "workspace": {"id": workspace_id},
            "plugins": [_serialize(p, workspace_id) for p in plugins],
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>", methods=["GET"])
@login_required
def api_inspect_plugin(workspace_id: int, plugin_id: str):
    resolve_workspace(workspace_id)
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        return jsonify({"error": "Plugin not found."}), 404
    return jsonify(_serialize(plugin, workspace_id))


# --------------------------------------------------------------------------
# API: trusted publisher key management (read-only, #386)
# --------------------------------------------------------------------------


@bp.route("/api/trusted-publishers", methods=["GET"])
@login_required
def api_list_trusted_publishers():
    """List the configured trusted publisher ids (never key material).

    The configured keys live in ``PLUGIN_TRUSTED_KEYS`` (an operator secret
    setting). This read-only surface lets an operator confirm *which* publisher
    ids are trusted and under which policy, without exposing any key bytes or
    fingerprints. Invalid key material is rejected when the application loads
    (``app.services.plugin_trust.validate_trust_config``).
    """
    publishers = trusted_publisher_ids(current_app.config.get("PLUGIN_TRUSTED_KEYS") or {})
    policy = current_app.config.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT)
    return jsonify(
        {
            "publishers": publishers,
            "count": len(publishers),
            "policy": policy,
        }
    )


# --------------------------------------------------------------------------
# API: plugin discovery (read-only, local-directory based)
# --------------------------------------------------------------------------


@bp.route("/discover", methods=["GET"])
@login_required
def api_discover_plugins():
    """List plugins found in the configured discovery directory (#172).

    Read-only: returns the id/name/version/description/capabilities of each
    manifest discovered in ``PLUGIN_DISCOVERY_DIR`` that is not already
    registered, without installing or loading anything.
    """
    return jsonify({"plugins": discover_plugins()})


# --------------------------------------------------------------------------
# API: install / enable / disable / capabilities (owner only)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins/install", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_install_plugin(workspace_id: int):
    """Install a trusted/local plugin manifest into the workspace.

    The plugin id is taken solely from the validated manifest (identity
    binding); a client cannot override it. Installation creates or reuses the
    global ``Plugin`` row and a per-workspace ``PluginInstallation``. It never
    loads or executes plugin code and never creates capability grants.
    """
    data = request.get_json(silent=True) or {}
    manifest_data = data.get("manifest")
    if not isinstance(manifest_data, dict):
        return jsonify({"error": "A manifest object is required."}), 400

    try:
        manifest = PluginManifest.from_dict(
            manifest_data,
            trusted_publishers=current_app.config["PLUGIN_TRUSTED_KEYS"],
            trust_policy=current_app.config["PLUGIN_TRUST_POLICY"],
        )
    except ManifestValidationError as exc:
        return jsonify({"error": f"Invalid plugin manifest: {exc}"}), 400
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400

    # Reject a plugin whose explicit PEP 440 compatibility range does not
    # include the running application version (an absent field is unrestricted).
    try:
        enforce_compatible(manifest.compatibility, subject=f"Plugin {manifest.id}")
    except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        plugin = _install_in_workspace(manifest, workspace_id)
    except PluginEntryPointMismatchError:
        db.session.rollback()
        return jsonify({"error": "Manifest entry_point does not match the registered plugin."}), 409
    except PluginAlreadyInstalledError:
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

    return jsonify(_serialize(plugin, workspace_id)), 201


@bp.route(
    "/api/workspaces/<int:workspace_id>/plugins/install-from-catalog",
    methods=["POST"],
)
@login_required
@require_workspace_capability("manage_plugins")
def api_install_plugin_from_catalog(workspace_id: int):
    """Install a plugin pinned by the operator-controlled catalog (#389).

    Owner-only. The request names a catalog entry (``plugin_id`` plus an
    optional ``version``); a client cannot supply a manifest, an artifact URL,
    or a publisher key. The entry is validated against the catalog, the
    manifest is re-validated against the manifest schema, and the active
    ``PLUGIN_TRUST_POLICY`` is enforced *before* anything is written:

    - unsigned entries are refused when the policy is ``required``;
    - a forged, malformed, or untrusted signature is always refused;
    - the artifact sha256 from the catalog is recorded in the response.

    Success registers metadata only: no artifact is downloaded, no plugin code
    is executed, and **no capability grant is created**. Grants remain explicit
    per workspace.
    """
    data = request.get_json(silent=True) or {}
    plugin_id = data.get("plugin_id")
    version = data.get("version")

    if not isinstance(plugin_id, str) or not plugin_id.strip():
        return jsonify({"error": "plugin_id is required."}), 400
    if version is not None and (not isinstance(version, str) or not version.strip()):
        return jsonify({"error": "version must be a non-empty string when provided."}), 400

    try:
        catalog = load_plugin_catalog(current_app.config)
    except CatalogUnavailableError as exc:
        return jsonify({"error": str(exc)}), 404
    except CatalogError as exc:
        return jsonify({"error": f"Invalid plugin catalog: {exc}"}), 400

    try:
        entry = catalog.get(
            plugin_id.strip(),
            version.strip() if version is not None else None,
        )
    except CatalogEntryNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except CatalogError as exc:
        return jsonify({"error": f"Invalid plugin version: {exc}"}), 400

    trusted_publishers = current_app.config.get("PLUGIN_TRUSTED_KEYS") or {}
    try:
        # Schema validation is trust-policy agnostic here: the detached (or
        # embedded) signature is enforced separately below so a catalog entry
        # signed out-of-band is not rejected as "unsigned".
        manifest = PluginManifest.from_dict(
            entry.manifest,
            trusted_publishers=trusted_publishers,
            trust_policy=VERIFY_IF_PRESENT,
        )
    except ManifestValidationError as exc:
        return jsonify({"error": f"Invalid plugin manifest: {exc}"}), 400
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400

    policy = current_app.config.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT)
    # A catalog entry may carry the publisher signature detached (catalog
    # field) or embedded in the manifest; both sign the same canonical body, so
    # the effective signature is whichever was supplied.
    signature = entry.signature if entry.signature is not None else entry.manifest.get("signature")
    try:
        trust = enforce_publisher_trust(
            entry.manifest,
            signature,
            trusted_publishers=trusted_publishers,
            policy=policy,
        )
    except PluginTrustConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    except PublisherSignatureError as exc:
        # Refusals are auditable but never store artifact bytes or key material.
        record_plugin_audit(
            workspace_id,
            "denied",
            actor=current_user,
            plugin_id=entry.plugin_id,
            outcome="denied",
            reason="publisher signature refused",
        )
        db.session.commit()
        return jsonify({"error": str(exc)}), 403

    try:
        enforce_compatible(manifest.compatibility, subject=f"Plugin {manifest.id}")
    except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
        return jsonify({"error": str(exc)}), 400

    # The verified trust fact is what gets persisted, not whatever the catalog
    # (or manifest) claimed.
    manifest.trust_state = trust.state
    manifest.trust_publisher = trust.publisher

    try:
        plugin = _install_in_workspace(manifest, workspace_id)
    except PluginEntryPointMismatchError:
        db.session.rollback()
        return jsonify({"error": "Manifest entry_point does not match the registered plugin."}), 409
    except PluginAlreadyInstalledError:
        return jsonify({"error": "Plugin is already installed in this workspace."}), 409

    payload = _serialize(plugin, workspace_id)
    payload["source"] = entry.source
    payload["integrity"] = entry.integrity
    return jsonify(payload), 201


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/update", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_update_plugin(workspace_id: int, plugin_id: str):
    """Update a registered plugin from a validated manifest (owner only).

    The manifest is fully validated and identity-bound: its ``id`` must match
    the plugin being updated and its ``entry_point`` must not change, so an
    update can never point an installed plugin at different code. The declared
    PEP 440 ``compatibility`` range is enforced before anything is written; an
    update that excludes the running application version is refused with
    ``400`` and a message naming both the range and the current version. The
    registered plugin (and every workspace installation) is left untouched.

    Capabilities and per-workspace configuration are not modified by an update;
    grants stay explicit and workspace-scoped.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404

    data = request.get_json(silent=True) or {}
    manifest_data = data.get("manifest")
    if not isinstance(manifest_data, dict):
        return jsonify({"error": "A manifest object is required."}), 400

    try:
        manifest = PluginManifest.from_dict(
            manifest_data,
            trusted_publishers=current_app.config["PLUGIN_TRUSTED_KEYS"],
            trust_policy=current_app.config["PLUGIN_TRUST_POLICY"],
        )
    except ManifestValidationError as exc:
        return jsonify({"error": f"Invalid plugin manifest: {exc}"}), 400
    except PluginError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        plugin = update_plugin(plugin_id, manifest)
    except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except PluginEntryPointMismatchError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 409
    except PluginUpdateError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except PluginNotFoundError:
        db.session.rollback()
        return jsonify({"error": "Plugin not found."}), 404

    return jsonify(_serialize(plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/enable", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_enable_plugin(workspace_id: int, plugin_id: str):
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    if not installation.enabled:
        # Resolve declared dependencies before enabling. The workspace
        # installation must not become enabled when a required dependency is
        # missing, disabled, incompatible, or cyclic.
        from app.services.plugin_ops import (
            PluginDependencyResolutionError,
            resolve_plugin_dependencies,
        )

        try:
            resolve_plugin_dependencies(installation.plugin)
        except PluginDependencyResolutionError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
        installation.enabled = True
        record_plugin_audit(
            workspace_id,
            "enabled",
            actor=current_user,
            plugin_id=plugin_id,
            outcome="success",
        )
        db.session.commit()
        emit_event(
            "plugin.enabled",
            data={"plugin_id": plugin_id},
            workspace_id=workspace_id,
            user_id=current_user.id,
        )
    else:
        db.session.commit()
    return jsonify(_serialize(installation.plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/disable", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_disable_plugin(workspace_id: int, plugin_id: str):
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    if installation.enabled:
        installation.enabled = False
        record_plugin_audit(
            workspace_id,
            "disabled",
            actor=current_user,
            plugin_id=plugin_id,
            outcome="success",
        )
        db.session.commit()
        emit_event(
            "plugin.disabled",
            data={"plugin_id": plugin_id},
            workspace_id=workspace_id,
            user_id=current_user.id,
        )
    else:
        db.session.commit()
    return jsonify(_serialize(installation.plugin, workspace_id))


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/uninstall", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_uninstall_plugin(workspace_id: int, plugin_id: str):
    """Uninstall a plugin from the workspace.

    Revokes every capability grant for that workspace, removes the per-workspace
    installation, and emits ``plugin.uninstalled``. The global ``Plugin`` row is
    kept so other workspaces' installations remain intact. Lifecycle code, when
    a plugin runs it, reacts to the ``plugin.uninstalled`` event; the management
    API never loads or executes plugin code itself.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    CapabilityStore.revoke_all(plugin_id, workspace_id)
    db.session.delete(installation)
    db.session.commit()
    emit_event(
        "plugin.uninstalled",
        data={"plugin_id": plugin_id},
        workspace_id=workspace_id,
        user_id=current_user.id,
    )
    return jsonify({"ok": True, "plugin_id": plugin_id})


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/capabilities", methods=["POST"])
@login_required
@require_workspace_capability("manage_plugins")
def api_update_capabilities(workspace_id: int, plugin_id: str):
    """Explicitly grant/revoke capabilities for a plugin in a workspace.

    Grants are never implicit: each requested capability must be declared in
    the plugin's manifest and is recorded through ``CapabilityStore``. Every
    successful grant/revoke and every rejected capability request is appended
    to the workspace audit trail (owner-visible only).
    """
    plugin = Plugin.query.filter_by(id=plugin_id).first()
    if plugin is None:
        return jsonify({"error": "Plugin not found."}), 404
    if _installation(plugin_id, workspace_id) is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404

    data = request.get_json(silent=True) or {}
    declared = set(plugin.capabilities or [])
    grant_names = data.get("grant") or []
    revoke_names = data.get("revoke") or []

    if not isinstance(grant_names, list) or not isinstance(revoke_names, list):
        return jsonify({"error": "grant and revoke must be lists."}), 400

    for raw in [*grant_names, *revoke_names]:
        cap_name = _capability_name(str(raw))
        if cap_name is None:
            record_plugin_audit(
                workspace_id,
                "denied",
                actor=current_user,
                plugin_id=plugin_id,
                capability=str(raw)[:64],
                outcome="denied",
                reason="capability request rejected",
            )
            db.session.commit()
            return jsonify({"error": f"Unknown capability: {raw}"}), 400
        if cap_name not in declared:
            record_plugin_audit(
                workspace_id,
                "denied",
                actor=current_user,
                plugin_id=plugin_id,
                capability=Capability[cap_name].value,
                outcome="denied",
                reason="capability not declared by plugin",
            )
            db.session.commit()
            return jsonify({"error": f"Capability {cap_name} is not declared by this plugin."}), 400

    for raw in grant_names:
        cap_name = _capability_name(str(raw))
        if cap_name is None or cap_name not in declared:
            continue
        value = Capability[cap_name].value
        if CapabilityStore.has_capability(plugin_id, workspace_id, value):
            continue
        CapabilityStore.grant(plugin_id, workspace_id, value, granted_by_id=current_user.id)
        record_plugin_audit(
            workspace_id,
            "granted",
            actor=current_user,
            plugin_id=plugin_id,
            capability=value,
            outcome="success",
        )
    for raw in revoke_names:
        cap_name = _capability_name(str(raw))
        if cap_name is None or cap_name not in declared:
            continue
        value = Capability[cap_name].value
        if CapabilityStore.revoke(plugin_id, workspace_id, value):
            record_plugin_audit(
                workspace_id,
                "revoked",
                actor=current_user,
                plugin_id=plugin_id,
                capability=value,
                outcome="success",
            )

    db.session.commit()
    return jsonify(_serialize(plugin, workspace_id))


# --------------------------------------------------------------------------
# API: per-workspace plugin configuration (owner only)
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/config", methods=["GET"])
@login_required
@require_workspace_capability("manage_plugins")
def api_get_plugin_config(workspace_id: int, plugin_id: str):
    """Read a plugin installation's workspace-scoped configuration.

    Owner-only: the config may hold secrets, so it is never exposed on the
    list/inspect surfaces (which omit ``config``) and never to non-owners.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    return jsonify(
        {
            "plugin_id": plugin_id,
            "workspace_id": workspace_id,
            "config": installation.config or {},
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/plugins/<plugin_id>/config", methods=["PUT"])
@login_required
@require_workspace_capability("manage_plugins")
def api_update_plugin_config(workspace_id: int, plugin_id: str):
    """Replace a plugin installation's workspace-scoped configuration.

    The replacement is validated against the manifest's declared ``configuration``
    when one is present (unknown keys and wrong-typed values are rejected with
    a 400); it is size/depth-bounded and never includes secrets from elsewhere.
    """
    installation = _installation(plugin_id, workspace_id)
    if installation is None:
        return jsonify({"error": "Plugin is not installed in this workspace."}), 404
    plugin = db.session.get(Plugin, plugin_id)

    data = request.get_json(silent=True) or {}
    proposed = data.get("config")
    if proposed is None:
        proposed = {}
    declared = (plugin.configuration if plugin is not None else None) or {}
    ok, reason = validate_plugin_config(declared, proposed)
    if not ok:
        return jsonify({"error": reason}), 400

    installation.config = proposed
    db.session.commit()
    return jsonify(
        {
            "plugin_id": plugin_id,
            "workspace_id": workspace_id,
            "config": installation.config or {},
        }
    )


@bp.route("/api/workspaces/<int:workspace_id>/plugin-errors", methods=["GET"])
@login_required
@require_workspace_capability("manage_plugins")
def api_list_plugin_errors(workspace_id: int):
    """Owner-scoped read of a workspace's structured plugin error reports.

    Reports are bounded (no stack traces or payloads) and only ever returned
    for the caller's own workspace (fail closed for non-owners).
    """
    reports = list_workspace_error_reports(workspace_id)
    return jsonify({"workspace_id": workspace_id, "count": len(reports), "errors": reports})
