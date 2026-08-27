"""Strict MiniQt view for ReplayLab's deterministic EID/pass tree.

This module is intentionally UI-only and Python 3.6 compatible.  It consumes
the fixed ``replaylab-pass-classification/v1`` model, creates buttons for exact
EventIds, and calls only the process-owner supplied ``set_event(int)`` callback.
No command, path, file, offset, binding, or replay mutation can be supplied by
the JSON model.
"""

from __future__ import print_function

import math
import re


CLASSIFICATION_SCHEMA = "replaylab-pass-classification/v1"
CLASSIFICATION_VERSION = 1
PANEL_STATUS_SCHEMA = "replaylab-pass-panel-status/v1"

MAX_NODES = 20000
MAX_COLLECTION = 100000
MAX_TEXT = 4096
MAX_LABEL = 512
MAX_NODE_ID = 128

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NODE_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")

CATEGORY_ORDER = (
    "Depth",
    "Shadow",
    "BasePass",
    "GBuffer",
    "Lighting",
    "Transparent",
    "Post",
    "UI",
    "Compute",
    "Utility",
    "Unknown",
)
CATEGORIES = set(CATEGORY_ORDER)
CONFIDENCE = set(("confirmed", "probable", "candidate"))

TOP_LEVEL_KEYS = set((
    "schema",
    "schemaVersion",
    "captureId",
    "generatedAt",
    "items",
    "containers",
    "tree",
    "omittedEvents",
    "summary",
    "policy",
    "provisionalOverlayCount",
))
REQUIRED_TOP_LEVEL_KEYS = set((
    "schema", "schemaVersion", "captureId", "items", "tree", "policy"))

# The panel never consumes these fields.  Rejecting them recursively prevents
# a future refactor from accidentally treating UI JSON as executable input.
FORBIDDEN_KEYS = set((
    "command",
    "commands",
    "cmd",
    "commandline",
    "executable",
    "argument",
    "arguments",
    "args",
    "environment",
    "env",
    "file",
    "filename",
    "filepath",
    "sourcepath",
    "candidatepath",
    "workspacepath",
    "projectpath",
    "assetpath",
    "directory",
    "offset",
    "byteoffset",
    "fieldoffset",
    "resourceoffset",
    "register",
    "registerspace",
    "binding",
    "bind",
    "slot",
    "base64",
    "bytes",
    "payload",
))


class PassPanelError(RuntimeError):
    pass


def _key_token(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _text(value, field, maximum=MAX_TEXT, allow_empty=False):
    if not isinstance(value, str):
        raise PassPanelError("%s must be text" % field)
    result = value.strip()
    if ((not result and not allow_empty) or len(result) > maximum or
            "\x00" in result):
        raise PassPanelError("%s has invalid content/length" % field)
    return result


def _integer(value, field, minimum=0, maximum=0xFFFFFFFF):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PassPanelError("%s must be an integer" % field)
    if value < minimum or value > maximum:
        raise PassPanelError("%s is out of range" % field)
    return value


def _strict_bool(value, field):
    if not isinstance(value, bool):
        raise PassPanelError("%s must be boolean" % field)
    return value


def _validate_json_safety(value, count, depth=0):
    if depth > 24:
        raise PassPanelError("classification nesting is too deep")
    count[0] += 1
    if count[0] > MAX_COLLECTION:
        raise PassPanelError("classification JSON is too large")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PassPanelError("classification contains a non-finite number")
        return
    if isinstance(value, str):
        _text(value, "classification text", MAX_TEXT, True)
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION:
            raise PassPanelError("classification list is too large")
        for item in value:
            _validate_json_safety(item, count, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION:
            raise PassPanelError("classification object is too large")
        for key, item in value.items():
            if not isinstance(key, str):
                raise PassPanelError("classification object keys must be text")
            _text(key, "classification key", 128)
            if _key_token(key) in FORBIDDEN_KEYS:
                raise PassPanelError(
                    "classification contains forbidden UI field: %s" % key)
            _validate_json_safety(item, count, depth + 1)
        return
    raise PassPanelError("classification contains a non-JSON value")


def _exact_keys(value, required, allowed, field):
    keys = set(value.keys())
    missing = required - keys
    unknown = keys - allowed
    if missing:
        raise PassPanelError(
            "%s is missing: %s" % (field, ", ".join(sorted(missing))))
    if unknown:
        raise PassPanelError(
            "%s contains unsupported fields: %s" %
            (field, ", ".join(sorted(unknown))))


def _proposal(value):
    if not isinstance(value, dict):
        raise PassPanelError("semanticProposal must be an object")
    required = set((
        "authority",
        "verified",
        "applied",
        "reviewRequired",
        "physicalEvidence",
        "proposedCategory",
        "proposedSubcategory",
        "displayLabel",
        "rationale",
    ))
    _exact_keys(value, required, required, "semanticProposal")
    if value.get("authority") != "AIProvisional":
        raise PassPanelError("semanticProposal must have AIProvisional authority")
    if (_strict_bool(value.get("verified"), "semanticProposal.verified") or
            _strict_bool(value.get("applied"), "semanticProposal.applied") or
            _strict_bool(value.get("physicalEvidence"),
                         "semanticProposal.physicalEvidence")):
        raise PassPanelError("AIProvisional proposal cannot be verified/applied/evidence")
    if not _strict_bool(value.get("reviewRequired"),
                        "semanticProposal.reviewRequired"):
        raise PassPanelError("AIProvisional proposal must require review")
    category = _text(
        value.get("proposedCategory", ""), "semanticProposal.proposedCategory",
        64, True)
    if category and category not in CATEGORIES:
        raise PassPanelError("semanticProposal category is unsupported")
    return {
        "authority": "AIProvisional",
        "proposedCategory": category,
        "proposedSubcategory": _text(
            value.get("proposedSubcategory", ""),
            "semanticProposal.proposedSubcategory", MAX_LABEL, True),
        "displayLabel": _text(
            value.get("displayLabel", ""),
            "semanticProposal.displayLabel", MAX_LABEL, True),
        "rationale": _text(
            value.get("rationale", ""),
            "semanticProposal.rationale", MAX_TEXT, True),
    }


def _node_id(value, seen):
    result = _text(value, "tree node id", MAX_NODE_ID)
    if not NODE_ID_RE.match(result):
        raise PassPanelError("tree node id must be lowercase hexadecimal")
    if result in seen:
        raise PassPanelError("tree contains duplicate node id")
    seen.add(result)
    return result


def _action_node(value, seen_ids, seen_events, node_count):
    if not isinstance(value, dict):
        raise PassPanelError("action tree node must be an object")
    required = set((
        "id", "nodeType", "eventId", "label", "confidence", "indirect"))
    _exact_keys(value, required, required, "action tree node")
    if value.get("nodeType") != "action":
        raise PassPanelError("tree leaf must have nodeType=action")
    event_id = _integer(value.get("eventId"), "action.eventId", 1)
    if event_id in seen_events:
        raise PassPanelError("tree contains duplicate EventId")
    seen_events.add(event_id)
    confidence = _text(value.get("confidence"), "action.confidence", 16)
    if confidence not in CONFIDENCE:
        raise PassPanelError("action confidence is unsupported")
    node_count[0] += 1
    return {
        "id": _node_id(value.get("id"), seen_ids),
        "nodeType": "action",
        "eventId": event_id,
        "label": _text(value.get("label"), "action.label", MAX_LABEL),
        "confidence": confidence,
        "indirect": _strict_bool(value.get("indirect"), "action.indirect"),
    }


def _group_node(value, seen_ids, seen_events, node_count):
    if not isinstance(value, dict):
        raise PassPanelError("tree group must be an object")
    required = set((
        "id", "nodeType", "groupKind", "label", "count",
        "foldByDefault", "children"))
    _exact_keys(value, required, required, "tree group")
    node_type = _text(value.get("nodeType"), "tree group nodeType", 32)
    if node_type not in ("container", "markerGroup"):
        raise PassPanelError("tree group nodeType is unsupported")
    group_kind = _text(value.get("groupKind"), "tree group kind", 32)
    if node_type == "container" and group_kind != "indirect-container":
        raise PassPanelError("container nodes must be indirect-container groups")
    if node_type == "markerGroup" and group_kind not in ("marker", "ungrouped"):
        raise PassPanelError("markerGroup kind is unsupported")
    children = value.get("children")
    if not isinstance(children, list) or not children:
        raise PassPanelError("tree group children must be a non-empty list")
    node_count[0] += 1
    result = {
        "id": _node_id(value.get("id"), seen_ids),
        "nodeType": node_type,
        "groupKind": group_kind,
        "label": _text(value.get("label"), "tree group label", MAX_LABEL),
        "foldByDefault": _strict_bool(
            value.get("foldByDefault"), "tree group foldByDefault"),
        "children": [],
    }
    for child in children:
        result["children"].append(
            _action_node(child, seen_ids, seen_events, node_count))
    count = _integer(value.get("count"), "tree group count", 1, MAX_NODES)
    if count != len(result["children"]):
        raise PassPanelError("tree group count does not match action leaves")
    result["count"] = count
    return result


def _subcategory_node(value, seen_ids, seen_events, node_count):
    if not isinstance(value, dict):
        raise PassPanelError("subcategory tree node must be an object")
    required = set(("id", "nodeType", "label", "count", "children"))
    _exact_keys(value, required, required, "subcategory tree node")
    if value.get("nodeType") != "subcategory":
        raise PassPanelError("subcategory nodeType is invalid")
    children = value.get("children")
    if not isinstance(children, list) or not children:
        raise PassPanelError("subcategory children must be a non-empty list")
    node_count[0] += 1
    result = {
        "id": _node_id(value.get("id"), seen_ids),
        "nodeType": "subcategory",
        "label": _text(value.get("label"), "subcategory label", MAX_LABEL),
        "children": [],
    }
    for child in children:
        result["children"].append(
            _group_node(child, seen_ids, seen_events, node_count))
    actual = sum(child["count"] for child in result["children"])
    count = _integer(value.get("count"), "subcategory count", 1, MAX_NODES)
    if count != actual:
        raise PassPanelError("subcategory count does not match action leaves")
    result["count"] = count
    return result


def _category_node(value, seen_ids, seen_events, node_count):
    if not isinstance(value, dict):
        raise PassPanelError("category tree node must be an object")
    required = set(("id", "nodeType", "label", "count", "children"))
    _exact_keys(value, required, required, "category tree node")
    if value.get("nodeType") != "category":
        raise PassPanelError("category nodeType is invalid")
    label = _text(value.get("label"), "category label", 64)
    if label not in CATEGORIES:
        raise PassPanelError("category label is unsupported")
    children = value.get("children")
    if not isinstance(children, list) or not children:
        raise PassPanelError("category children must be a non-empty list")
    node_count[0] += 1
    result = {
        "id": _node_id(value.get("id"), seen_ids),
        "nodeType": "category",
        "label": label,
        "children": [],
    }
    for child in children:
        result["children"].append(
            _subcategory_node(child, seen_ids, seen_events, node_count))
    actual = sum(child["count"] for child in result["children"])
    count = _integer(value.get("count"), "category count", 1, MAX_NODES)
    if count != actual:
        raise PassPanelError("category count does not match action leaves")
    result["count"] = count
    return result


def validate_classification(value, maximum_nodes=MAX_NODES):
    """Return a display-only copy of an exact pass-classification document."""

    maximum_nodes = _integer(maximum_nodes, "maximum_nodes", 1, MAX_NODES)
    if not isinstance(value, dict):
        raise PassPanelError("classification must be an object")
    _validate_json_safety(value, [0])
    _exact_keys(value, REQUIRED_TOP_LEVEL_KEYS, TOP_LEVEL_KEYS, "classification")
    if (value.get("schema") != CLASSIFICATION_SCHEMA or
            value.get("schemaVersion") != CLASSIFICATION_VERSION):
        raise PassPanelError("classification schema/version is unsupported")
    capture_id = _text(value.get("captureId"), "captureId", 64)
    if not SHA256_RE.match(capture_id):
        raise PassPanelError("captureId must be a lowercase SHA-256")
    policy = value.get("policy")
    if not isinstance(policy, dict):
        raise PassPanelError("classification policy must be an object")
    if (policy.get("classificationAuthority") != "FixedProgram" or
            policy.get("aiCanChangeFixedTree") is not False or
            policy.get("aiProposalAuthority") != "AIProvisional"):
        raise PassPanelError("classification policy does not preserve authority boundaries")

    tree = value.get("tree")
    items = value.get("items")
    if not isinstance(tree, list) or not isinstance(items, list):
        raise PassPanelError("classification tree/items must be lists")
    if len(items) > maximum_nodes:
        raise PassPanelError("classification action count exceeds the panel limit")
    seen_ids = set()
    seen_events = set()
    node_count = [0]
    clean_tree = []
    previous_category = -1
    for node in tree:
        clean = _category_node(node, seen_ids, seen_events, node_count)
        category_index = CATEGORY_ORDER.index(clean["label"])
        if category_index <= previous_category:
            raise PassPanelError("categories are not in canonical order")
        previous_category = category_index
        clean_tree.append(clean)
    if node_count[0] > maximum_nodes:
        raise PassPanelError("classification tree exceeds the panel node limit")

    item_by_event = {}
    provisional_count = 0
    for item in items:
        if not isinstance(item, dict):
            raise PassPanelError("classification item must be an object")
        event_id = _integer(item.get("eventId"), "item.eventId", 1)
        if event_id in item_by_event:
            raise PassPanelError("classification items contain duplicate EventId")
        category = _text(item.get("category"), "item.category", 64)
        if category not in CATEGORIES:
            raise PassPanelError("classification item category is unsupported")
        confidence = _text(item.get("confidence"), "item.confidence", 16)
        if confidence not in CONFIDENCE:
            raise PassPanelError("classification item confidence is unsupported")
        clean_item = {
            "eventId": event_id,
            "name": _text(item.get("name"), "item.name", MAX_LABEL),
            "category": category,
            "subcategory": _text(
                item.get("subcategory"), "item.subcategory", MAX_LABEL),
            "confidence": confidence,
            "semanticProposal": None,
        }
        if "semanticProposal" in item:
            clean_item["semanticProposal"] = _proposal(item["semanticProposal"])
            provisional_count += 1
        item_by_event[event_id] = clean_item
    if set(item_by_event.keys()) != seen_events:
        raise PassPanelError("classification tree leaves do not match item EventIds")

    for category in clean_tree:
        for subcategory in category["children"]:
            for group in subcategory["children"]:
                for leaf in group["children"]:
                    item = item_by_event[leaf["eventId"]]
                    if (item["category"] != category["label"] or
                            item["subcategory"] != subcategory["label"] or
                            item["confidence"] != leaf["confidence"] or
                            item["name"] != leaf["label"]):
                        raise PassPanelError(
                            "classification item and tree leaf disagree")

    declared_provisional = value.get("provisionalOverlayCount", provisional_count)
    declared_provisional = _integer(
        declared_provisional, "provisionalOverlayCount", 0, maximum_nodes)
    if declared_provisional != provisional_count:
        raise PassPanelError("provisionalOverlayCount does not match item overlays")
    return {
        "schema": CLASSIFICATION_SCHEMA,
        "schemaVersion": CLASSIFICATION_VERSION,
        "captureId": capture_id,
        "tree": clean_tree,
        "items": item_by_event,
        "actionCount": len(item_by_event),
        "nodeCount": node_count[0],
        "provisionalOverlayCount": provisional_count,
    }


class MiniQtPassPanel(object):
    """Collapsible, capture-bound pass tree for qrenderdoc MiniQt."""

    def __init__(self, ctx, qrd_module, set_event, closed_callback=None,
                 maximum_nodes=MAX_NODES):
        if not callable(set_event):
            raise PassPanelError("set_event must be a process-owner callback")
        self.maximum_nodes = _integer(
            maximum_nodes, "maximum_nodes", 1, MAX_NODES)
        self.ctx = ctx
        self.qrd = qrd_module
        self.set_event = set_event
        self.closed_callback = closed_callback
        self.mqt = ctx.Extensions().GetMiniQtHelper()
        self.top = self.mqt.CreateToplevelWidget(
            "ReplayLab Semantic Passes", self._closed)
        self.root = self.mqt.CreateVerticalContainer()
        self.mqt.AddWidget(self.top, self.root)
        self.header = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(
            self.header,
            "Fixed-program EID grouping · AI labels remain provisional")
        self.mqt.AddWidget(self.root, self.header)
        self.content = self.mqt.CreateVerticalContainer()
        self.mqt.AddWidget(self.root, self.content)
        self.status_label = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(self.status_label, "No pass classification loaded")
        self.mqt.AddWidget(self.root, self.status_label)
        self._docked = False
        self._closed_notified = False
        self._shutdown = False
        self._generation = 0
        self._capture_id = ""
        self._model = None
        self._dynamic_roots = []
        self._event_buttons = {}

    def _set_status(self, value):
        self.mqt.SetWidgetText(self.status_label, str(value))

    def _destroy_dynamic(self):
        self.mqt.ClearContainedWidgets(self.content)
        for widget in self._dynamic_roots:
            try:
                self.mqt.DestroyWidget(widget)
            except Exception:
                pass
        self._dynamic_roots = []
        self._event_buttons = {}

    def _expire(self, message, capture_id=""):
        self._generation += 1
        self._model = None
        self._capture_id = capture_id
        self._destroy_dynamic()
        empty = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(empty, message)
        self.mqt.AddWidget(self.content, empty)
        self._dynamic_roots.append(empty)
        self._set_status(message)

    def _closed(self, _ctx, _widget, _text_value):
        # MiniQt invokes this callback from QWidget::destroyed. At that point
        # ``top`` and its child widgets are already being torn down, so calling
        # any MiniQt function here (ClearContainedWidgets, SetWidgetText, ...)
        # can dereference a deleted QWidget and terminate qrenderdoc. Invalidate
        # only the Python-side identity and let Qt own the destruction.
        if self.top is None and self._closed_notified:
            return
        self._shutdown = True
        self._generation += 1
        self._model = None
        self._capture_id = ""
        self._dynamic_roots = []
        self._event_buttons = {}
        self._docked = False
        callback = None
        if self.closed_callback and not self._closed_notified:
            self._closed_notified = True
            callback = self.closed_callback
        # Drop every borrowed SWIG/native handle while MiniQt and the capture
        # context are still alive.  qrenderdoc does not call extension
        # unregister() on its ordinary process-exit path, so leaving these
        # wrappers for Py_Finalize gives Python 3.6 an unsafe destruction order.
        self.closed_callback = None
        self.top = None
        self.root = None
        self.header = None
        self.content = None
        self.status_label = None
        self.ctx = None
        self.qrd = None
        self.set_event = None
        self.mqt = None
        if callback:
            callback()

    def show(self):
        if self._shutdown:
            raise PassPanelError("pass panel is shut down")
        if not self._docked:
            reference = None
            dock_reference = self.qrd.DockReference.MainToolArea
            try:
                if self.ctx.HasEventBrowser():
                    reference = self.ctx.GetEventBrowser().Widget()
                    dock_reference = self.qrd.DockReference.RightOf
            except Exception:
                reference = None
                dock_reference = self.qrd.DockReference.MainToolArea
            self.ctx.AddDockWindow(
                self.top, dock_reference, reference, 0.32)
            self._docked = True
        self.ctx.RaiseDockWindow(self.top)
        return True

    def _action_clicked(self, event_id, capture_id, generation):
        # A queued click can arrive after the top-level QWidget's destroyed
        # callback. Never try to update a status label after that point.
        if self._shutdown:
            return False
        if (self._model is None or
                generation != self._generation or
                capture_id != self._capture_id):
            self._set_status("Ignored expired EID selection")
            return False
        try:
            self.set_event(int(event_id))
            self._set_status("Selected EID %d" % event_id)
            return True
        except Exception as exc:
            self._set_status("Could not select EID %d: %s" %
                             (event_id, str(exc)))
            return False

    def _add_action(self, parent, leaf, item, capture_id, generation):
        row = self.mqt.CreateHorizontalContainer()

        def _clicked(_ctx, _widget, _text_value, eid=leaf["eventId"],
                     cid=capture_id, gen=generation):
            self._action_clicked(eid, cid, gen)

        button = self.mqt.CreateButton(_clicked)
        prefix = "Indirect " if leaf.get("indirect") else ""
        self.mqt.SetWidgetText(
            button, "%sEID %d · %s" %
            (prefix, leaf["eventId"], leaf["label"]))
        self.mqt.AddWidget(row, button)
        confidence = self.mqt.CreateLabel()
        self.mqt.SetWidgetText(
            confidence, "Fixed: %s" % leaf["confidence"])
        self.mqt.AddWidget(row, confidence)
        proposal = item.get("semanticProposal")
        if proposal:
            label = (proposal.get("displayLabel") or
                     proposal.get("proposedSubcategory") or
                     proposal.get("proposedCategory") or "review candidate")
            provisional = self.mqt.CreateLabel()
            self.mqt.SetWidgetText(
                provisional, "AIProvisional: %s" % label)
            self.mqt.AddWidget(row, provisional)
        self.mqt.AddWidget(parent, row)
        self._event_buttons[leaf["eventId"]] = button

    def _rebuild(self):
        self._destroy_dynamic()
        if self._model is None:
            return
        generation = self._generation
        capture_id = self._capture_id
        for category in self._model["tree"]:
            category_group = self.mqt.CreateGroupBox(True)
            self.mqt.SetWidgetText(
                category_group, "%s (%d)" %
                (category["label"], category["count"]))
            category_body = self.mqt.CreateVerticalContainer()
            self.mqt.AddWidget(category_group, category_body)
            self.mqt.AddWidget(self.content, category_group)
            self._dynamic_roots.append(category_group)
            for subcategory in category["children"]:
                subcategory_group = self.mqt.CreateGroupBox(True)
                self.mqt.SetWidgetText(
                    subcategory_group, "%s (%d)" %
                    (subcategory["label"], subcategory["count"]))
                subcategory_body = self.mqt.CreateVerticalContainer()
                self.mqt.AddWidget(subcategory_group, subcategory_body)
                self.mqt.AddWidget(category_body, subcategory_group)
                for group in subcategory["children"]:
                    marker_group = self.mqt.CreateGroupBox(True)
                    prefix = "ExecuteIndirect · " if (
                        group["groupKind"] == "indirect-container") else ""
                    self.mqt.SetWidgetText(
                        marker_group, "%s%s (%d)" %
                        (prefix, group["label"], group["count"]))
                    marker_body = self.mqt.CreateVerticalContainer()
                    self.mqt.AddWidget(marker_group, marker_body)
                    self.mqt.AddWidget(subcategory_body, marker_group)
                    for leaf in group["children"]:
                        self._add_action(
                            marker_body,
                            leaf,
                            self._model["items"][leaf["eventId"]],
                            capture_id,
                            generation)
                    set_checked = getattr(self.mqt, "SetWidgetChecked", None)
                    if callable(set_checked):
                        # CollapseGroupBox maps its checked state to collapsed.
                        set_checked(marker_group, group["foldByDefault"])

    def load(self, classification):
        if self._shutdown:
            raise PassPanelError("pass panel is shut down")
        # Validate the complete document before expiring the current model.
        # A malformed new payload cannot destroy a still-valid view.
        model = validate_classification(classification, self.maximum_nodes)
        capture_changed = bool(
            self._capture_id and self._capture_id != model["captureId"])
        if capture_changed:
            self._expire(
                "Capture changed; previous pass classification expired",
                model["captureId"])
        self._generation += 1
        self._capture_id = model["captureId"]
        self._model = model
        self._rebuild()
        self._set_status(
            "Loaded %d EID(s), %d category group(s), %d AIProvisional label(s) · capture %s" %
            (model["actionCount"], len(model["tree"]),
             model["provisionalOverlayCount"], model["captureId"][:12]))
        return {
            "schema": PANEL_STATUS_SCHEMA,
            "captureId": model["captureId"],
            "actionCount": model["actionCount"],
            "nodeCount": model["nodeCount"],
            "categoryCount": len(model["tree"]),
            "provisionalOverlayCount": model["provisionalOverlayCount"],
            "captureChanged": capture_changed,
        }

    def capture_changed(self, capture_id=""):
        # CaptureViewer notifications can race with a dock close. A closed
        # MiniQt panel has no valid widgets left to rebuild or update.
        if self._shutdown:
            return False
        if capture_id:
            capture_id = _text(capture_id, "captureId", 64)
            if not SHA256_RE.match(capture_id):
                raise PassPanelError("captureId must be a lowercase SHA-256")
        self._expire(
            "Capture changed; pass classification must be regenerated",
            capture_id)
        return True

    def shutdown(self):
        if self._shutdown and self.top is None:
            return
        top = self.top
        mqt = self.mqt
        was_docked = self._docked
        self._shutdown = True
        self._generation += 1
        self._model = None
        self._capture_id = ""
        self._dynamic_roots = []
        self._event_buttons = {}
        try:
            if top is not None and mqt is not None:
                if was_docked:
                    # ToolWindowManager owns a docked widget and deletes it
                    # synchronously through this path.
                    mqt.CloseToplevelWidget(top)
                else:
                    # CloseToplevelWidget is a no-op for the undocked panel
                    # created during extension registration. Schedule explicit
                    # recursive destruction while Qt/Python are both alive.
                    mqt.DestroyWidget(top)
        except Exception:
            pass
        # An undocked DestroyWidget callback is deferred. Invalidate Python's
        # native handles now; the later QWidget::destroyed callback is
        # deliberately idempotent.
        self._closed(None, top, "")


__all__ = [
    "CLASSIFICATION_SCHEMA",
    "CLASSIFICATION_VERSION",
    "PANEL_STATUS_SCHEMA",
    "PassPanelError",
    "validate_classification",
    "MiniQtPassPanel",
]
