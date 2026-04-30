# tools/digitizer/panel.py

"""
tools/digitizer/panel.py

UI Panel for the Geological Map Digitizer tool.
"""

from PyQt5.QtWidgets import ( # type: ignore
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QSpinBox, QGroupBox, QLabel,
    QTreeWidget, QTreeWidgetItem, QLineEdit, QWidget
)
from PyQt5.QtCore import Qt, QCoreApplication # type: ignore
from PyQt5.QtGui import QColor # type: ignore

from qgis.gui import QgsMapLayerComboBox # type: ignore
from qgis.core import ( # type: ignore
    QgsMapLayerProxyModel, QgsVectorLayer, QgsFeature, 
    QgsField, QgsFields, QgsProject, QgsCategorizedSymbolRenderer,
    QgsRendererCategory, QgsFillSymbol
)
from PyQt5.QtCore import QVariant # type: ignore

from ...base.base_panel import BasePanel, ComputeWorker
from .engine import DigitizerEngine

def tr(message: str) -> str:
    return QCoreApplication.translate("RockMorph", message)

class DigitizerPanel(BasePanel):
    
    def __init__(self, iface, parent=None):
        self._engine = DigitizerEngine()
        self._results = None
        super().__init__(iface, parent)

    # ------------------------------------------------------------------
    # BasePanel Hooks
    # ------------------------------------------------------------------
    def _html_file(self) -> str:
        # No web view needed for this tool, everything happens on the QGIS Map
        return ""

    def _load_html(self) -> None:
        pass # Bypass web engine loading

    def _build_ui(self):
        root = QVBoxLayout(self._inner)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── Inputs ──────────────────────────────────────────────────
        input_group = QGroupBox(tr("Inputs"))
        input_layout = QFormLayout(input_group)

        self.raster_combo = QgsMapLayerComboBox()
        self.raster_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        input_layout.addRow(tr("Geological Map (Raster):"), self.raster_combo)

        area_row = QWidget()
        area_layout = QHBoxLayout(area_row)
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_layout.setSpacing(4)

        self.poly_combo = QgsMapLayerComboBox()

        self.poly_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer | QgsMapLayerProxyModel.LineLayer)
        self.poly_combo.setAllowEmptyLayer(True)
        self.poly_combo.setCurrentIndex(0)  # Vide par défaut
        self.poly_combo.setToolTip(tr(
            "<b>Study Area Boundary (Optional):</b><br>"
            "A Polygon or Line layer defining the area to digitize.<br>"
            "If a Line is provided, it will be automatically closed into a perimeter.<br>"
            "If left empty, the entire raster extent will be digitized."
        ))

        self._area_auto_label = QLabel(tr("full extent"))
        self._area_auto_label.setStyleSheet("color: #27ae60; font-size: 12px; font-style: italic;")
        self.poly_combo.layerChanged.connect(self._on_area_changed)

        area_layout.addWidget(self.poly_combo, stretch=1)
        area_layout.addWidget(self._area_auto_label)
        input_layout.addRow(tr("Study Area (Poly/Line):"), area_row)

        root.addWidget(input_group)

        # ── Settings ────────────────────────────────────────────────
        param_group = QGroupBox(tr("Digitization Settings"))
        param_layout = QFormLayout(param_group)

        self.spin_clusters = QSpinBox()
        self.spin_clusters.setRange(2, 50)
        self.spin_clusters.setValue(8)
        self.spin_clusters.setToolTip(tr(
            "<b>Number of Colors (K):</b><br>"
            "Set this slightly higher than the number of geological formations to account "
            "for shadows, lines, and scanned map noise."
        ))
        param_layout.addRow(tr("Number of colors (clusters):"), self.spin_clusters)

        self.spin_smooth = QSpinBox()
        self.spin_smooth.setRange(1, 21)
        self.spin_smooth.setSingleStep(2) # Only odd numbers
        self.spin_smooth.setValue(5)
        self.spin_smooth.setToolTip(tr(
            "<b>Smoothing Level:</b><br>"
            "Median filter kernel size (must be odd). Higher values remove more noise "
            "and text, but may round off geological boundaries."
        ))
        param_layout.addRow(tr("Noise smoothing (pixels):"), self.spin_smooth)


        self.spin_sieve = QSpinBox()
        self.spin_sieve.setRange(0, 1000000)  
        self.spin_sieve.setSingleStep(500)
        self.spin_sieve.setValue(5000)        
        self.spin_sieve.setToolTip(tr(
            "<b>Sieve Filter Threshold:</b><br>"
            "Minimum size (in pixels) for a polygon to be kept. "
            "Polygons smaller than this (like text, grids, or noise) will be absorbed "
            "by the surrounding geological unit. Set to 0 to disable."
        ))
        param_layout.addRow(tr("Minimum polygon size (px):"), self.spin_sieve)

        root.addWidget(param_group)

        # ── Compute Button ──────────────────────────────────────────
        self.compute_btn = QPushButton(tr("Digitize Map"))
        self.compute_btn.setFixedHeight(36)
        self.compute_btn.setStyleSheet("""
            QPushButton { background-color: #2d6a9f; color: white; border-radius: 4px; font-weight: bold; }
            QPushButton:hover   { background-color: #3a7fc1; }
            QPushButton:pressed { background-color: #1f4f7a; }
        """)
        self.compute_btn.clicked.connect(self._on_compute)
        root.addWidget(self.compute_btn)
        root.addWidget(self._progress_container)

        # ── Results & Map Output ────────────────────────────────────
        out_group = QGroupBox(tr("Map Output"))
        out_layout = QVBoxLayout(out_group)
        
        # Color preview tree
        self.color_tree = QTreeWidget()
        self.color_tree.setHeaderLabels([tr("Cluster ID"), tr("Extracted Color")])
        self.color_tree.setFixedHeight(150)
        out_layout.addWidget(self.color_tree)

        form = QFormLayout()
        self.layer_name_edit = QLineEdit("Digitized_Geology")
        form.addRow(tr("Output layer name:"), self.layer_name_edit)
        out_layout.addLayout(form)

        self.add_map_btn = QPushButton(tr("Add to QGIS Map"))
        self.add_map_btn.setEnabled(False)
        self.add_map_btn.clicked.connect(self._add_layer_to_map)
        out_layout.addWidget(self.add_map_btn)

        root.addWidget(out_group)
        root.addStretch()
    
    def _on_area_changed(self, layer):
        is_auto = (layer is None or not layer.isValid())
        self._area_auto_label.setVisible(is_auto)

    # ------------------------------------------------------------------
    # Compute Logic
    # ------------------------------------------------------------------
    def _on_compute(self):
        if not self._engine.validate(
            raster_layer=self.raster_combo.currentLayer(),
            polygon_layer=self.poly_combo.currentLayer()
        ):
            self.show_error(tr("Please select a valid raster and polygon layer."))
            return
        
        poly_layer = self.poly_combo.currentLayer()
        params = {
            "raster_layer": self.raster_combo.currentLayer(),
            "polygon_layer": poly_layer if (poly_layer and poly_layer.isValid()) else None,
            "n_clusters": self.spin_clusters.value(),
            "smooth_size": self.spin_smooth.value(),
            "sieve_threshold": self.spin_sieve.value()
        }

        self.compute_btn.setEnabled(False)
        self.set_loading_state(True, tr("Starting digitization..."))

        self._worker = ComputeWorker(self._engine, params)
        self._worker.progress.connect(self.update_progress)
        self._worker.finished.connect(self._on_compute_finished)
        self._worker.error.connect(self._on_compute_error)
        self._worker.start()

    def _on_compute_finished(self, result: dict):
        self.compute_btn.setEnabled(True)
        self.set_loading_state(False)
        
        self._results = result
        polys = result.get("polygons",[])
        
        if not polys:
            self.show_error(tr("No polygons generated. Check your inputs."))
            return

        self._populate_tree()
        self.add_map_btn.setEnabled(True)
        self._add_layer_to_map()
        
        self.show_info(tr(f"Digitization complete. {len(polys)} polygons generated."))

    def _on_compute_error(self, message: str):
        self.compute_btn.setEnabled(True)
        self.set_loading_state(False)
        self.show_error(message)

    def _on_result(self, data: dict) -> None:
        pass # Not using WebEngine

    # ------------------------------------------------------------------
    # UI Updates
    # ------------------------------------------------------------------
    def _populate_tree(self):
        self.color_tree.clear()
        colors = self._results.get("colors",[])
        
        for i, hex_color in enumerate(colors):
            item = QTreeWidgetItem(self.color_tree)
            item.setText(0, f"Cluster {i}")
            
            # Create a small color block
            color = QColor(hex_color)
            item.setBackground(1, color)
            # Make text readable depending on background darkness
            text_color = Qt.white if color.value() < 128 else Qt.black
            item.setForeground(1, text_color)
            item.setText(1, hex_color)

    # ------------------------------------------------------------------
    # QGIS Map Integration (The "Pro" Touch)
    # ------------------------------------------------------------------
    def _add_layer_to_map(self):
        if not self._results:
            return

        layer_name = self.layer_name_edit.text() or "Digitized_Geology"
        crs_wkt = self._results["crs_wkt"]
        
        # Remove existing layer with the same name
        for lyr in QgsProject.instance().mapLayersByName(layer_name):
            QgsProject.instance().removeMapLayer(lyr.id())

        # Create memory layer
        vl = QgsVectorLayer(f"MultiPolygon?crs={crs_wkt}", layer_name, "memory")
        pr = vl.dataProvider()

        # Add fields
        fields = QgsFields()
        fields.append(QgsField("cluster_id", QVariant.Int))
        fields.append(QgsField("hex_color", QVariant.String))
        pr.addAttributes(fields)
        vl.updateFields()

        # Add features
        colors = self._results["colors"]
        features =[]
        for poly_data in self._results["polygons"]:
            feat = QgsFeature(fields)
            feat.setGeometry(poly_data["geometry"])
            c_id = poly_data["cluster_id"]
            feat.setAttributes([c_id, colors[c_id]])
            features.append(feat)

        pr.addFeatures(features)
        vl.updateExtents()

        # Build Categorized Symbology automatically!
        categories =[]
        for i, hex_color in enumerate(colors):
            symbol = QgsFillSymbol.createSimple({
                "color": hex_color,
                "outline_color": "#333333",
                "outline_width": "0.1"
            })
            category = QgsRendererCategory(i, symbol, f"Cluster {i}")
            categories.append(category)

        renderer = QgsCategorizedSymbolRenderer("cluster_id", categories)
        vl.setRenderer(renderer)
        vl.triggerRepaint()

        # Add to map
        QgsProject.instance().addMapLayer(vl)