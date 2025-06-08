$(function () {
  function PinputShapingViewModel(parameters) {
    const self = this;

    self.settingsViewModel = parameters[0];

    // Test status
    self.samples = ko.observableArray([]);
    self.summary = ko.observable("");
    self.hasData = ko.observable(false);
    self.error = ko.observable(null);

    // Settings
    self.sizeX = ko.observable(0);
    self.sizeY = ko.observable(0);
    self.sizeZ = ko.observable(0);
    self.accelMin = ko.observable(0);
    self.accelMax = ko.observable(0);
    self.freqStart = ko.observable(0);
    self.freqEnd = ko.observable(0);
    self.dampingRatio = ko.observable("");
    self.sensorType = ko.observable("");

    // Save settings before save
    self.onSettingsBeforeSave = function () {
      const s = self.settingsViewModel.settings.plugins.Pinput_Shaping;
      s.sizeX(self.sizeX());
      s.sizeY(self.sizeY());
      s.sizeZ(self.sizeZ());
      s.accelMin(self.accelMin());
      s.accelMax(self.accelMax());
      s.freqStart(self.freqStart());
      s.freqEnd(self.freqEnd());
      s.dampingRatio(self.dampingRatio());
      s.sensorType(self.sensorType());
    };

    // On startup
    self.onStartupComplete = function () {
      const s = self.settingsViewModel.settings.plugins.Pinput_Shaping;
      if (!s) {
        console.warn("⚠️ Plugin settings not found.");
        return;
      }

      self.sizeX(s.sizeX());
      self.sizeY(s.sizeY());
      self.sizeZ(s.sizeZ());
      self.accelMin(s.accelMin());
      self.accelMax(s.accelMax());
      self.freqStart(s.freqStart());
      self.freqEnd(s.freqEnd());
      self.dampingRatio(s.dampingRatio());
      self.sensorType(s.sensorType());

      const profile = self.settingsViewModel.printerProfiles.currentProfileData;
      if (profile && profile.volume) {
        if (!self.sizeX() || self.sizeX() <= 0) self.sizeX(profile.volume.width);
        if (!self.sizeY() || self.sizeY() <= 0) self.sizeY(profile.volume.depth);
        if (!self.sizeZ() || self.sizeZ() <= 0) self.sizeZ(profile.volume.height);
      }
    };

    // ---Grid selection ---
    self.gridRows = ko.observableArray(
      Array.from({ length: 3 }, (_, row) =>
        Array.from({ length: 3 }, (_, col) => ({ row, col }))
      )
    );

    self.hasError = ko.computed(() => !!self.error());
    self.selectedCell = ko.observable({ row: 1, col: 1 }); // Center
    self.selectedZ = ko.observable(10); // Default Z

    self.cellWidth = ko.computed(() => self.sizeX() / 3);
    self.cellHeight = ko.computed(() => self.sizeY() / 3);

    self.calculatedX = ko.computed(() => {
      const cell = self.selectedCell();
      return Math.round((cell.col + 0.5) * self.cellWidth());
    });

    self.calculatedY = ko.computed(() => {
      const cell = self.selectedCell();
      return Math.round((cell.row + 0.5) * self.cellHeight());
    });

    // === Actions ===

    self.getShaperTooltip = function(name) {
      const tooltips = {
        ZV: "Zero Vibration – Simple and quick shaper",
        MZV: "Modified ZV – Better vibration suppression",
        EI: "Extra Insensitive – Good for noisy systems",
        "2HUMP_EI": "Extended EI with stronger suppression",
        "3HUMP_EI": "High-order EI for aggressive control"
      };
      return tooltips[name] || "Shaper configuration";
    };

    // Results Section Observables
    self.showResults = ko.observable(false);
    self.signalImgUrl = ko.observable("");
    self.psdImgUrl = ko.observable("");
    self.csvFileUrl = ko.observable("");
    self.recommendedCommand = ko.observable("");

    self.runAccTest = function () {
      self.error("");
      self.summary("Executing Test...");
      self.samples([]);
      self.hasData(false);

      OctoPrint.simpleApiCommand("Pinput_Shaping", "run_adxl_test", {})
        .done(function (response) {
          if (response.success) {
            self.summary(response.summary);
            self.samples(response.samples);
            self.hasData(true);
          } else {
            self.summary("");
            self.error("Error: " + response.error);
          }
        })
        .fail(function (jqXHR, textStatus, errorThrown) {
          self.summary("");
          self.error("AJAX error: " + errorThrown);
        });
    };

    self.shaperResults = ko.observableArray([]);

    self.openImageModal = function (imgUrl) {
      $("#modalImage").attr("src", imgUrl);
      $("#downloadImageLink").attr("href", imgUrl);
      $("#imageModal").modal("show");
    };

    self.runAxisTest = function (axis) {
      self.error(null);
      self.summary("Executing Test for " + axis + "...");
      self.hasData(false);

      OctoPrint.simpleApiCommand("Pinput_Shaping", "run_axis_test", { data: { axis: axis } })
        .done(function (response) {
          if (response.success) {
            self.summary(response.summary);
          } else {
            self.summary("");
            self.error("Error: " + response.error);
          }
        })
        .fail(function (jqXHR, textStatus, errorThrown) {
          self.summary("");
          self.error("AJAX error: " + errorThrown);
        });
    };

    self.runResonance = function (axis) {
      self.error(null);
      self.summary("Executing Resonance Test for " + axis + "...");
      self.hasData(false);

      const payload = {
        axis: axis,
        start_x: self.calculatedX(),
        start_y: self.calculatedY(),
        start_z: self.selectedZ()
      };

      console.log("🔁 Sending resonance test with payload:", payload);

      OctoPrint.simpleApiCommand("Pinput_Shaping", "run_resonance_test", { data: payload })
        .done(function (response) {
          if (response.success) {
            self.summary(response.summary);
          } else {
            self.summary("");
            self.error("Error: " + response.error);
          }
        })
        .fail(function (jqXHR, textStatus, errorThrown) {
          self.summary("");
          self.error("AJAX error: " + errorThrown);
        });
    };

    self.emergencyStop = function () {
      if (confirm("Are you sure? This will halt the printer immediately!")) {
        $.ajax({
          url: API_BASEURL + "printer/command",
          type: "POST",
          dataType: "json",
          contentType: "application/json; charset=UTF-8",
          data: JSON.stringify({ command: "M112" })
        }).fail(function (jqXHR, textStatus, errorThrown) {
          console.error("Emergency stop failed:", errorThrown);
        });
      }
    };

    self.sendFreqGcode = function () {
      const axis = self.bestShaperAxis().toUpperCase();
      const freq = parseFloat(self.baseFreq()).toFixed(2);
      const gcode = `M593 ${axis} F${freq}`;
    
      if (confirm(`Send to printer?\n\n${gcode}`)) {
        $.ajax({
          url: API_BASEURL + "printer/command",
          type: "POST",
          dataType: "json",
          contentType: "application/json; charset=UTF-8",
          data: JSON.stringify({ commands: [gcode] })
        })
          .done(() => alert("Frequency set!"))
          .fail(() => alert("Failed to send G-code"));
      }
    };
    
    self.sendDampingGcode = function () {
      const axis = self.bestShaperAxis().toUpperCase();
      const damping = parseFloat(self.dampingRatio()).toFixed(2);
      const gcode = `M593 ${axis} D${damping}`;
    
      if (confirm(`Send to printer?\n\n${gcode}`)) {
        $.ajax({
          url: API_BASEURL + "printer/command",
          type: "POST",
          dataType: "json",
          contentType: "application/json; charset=UTF-8",
          data: JSON.stringify({ commands: [gcode] })
        })
          .done(() => alert("Damping set!"))
          .fail(() => alert("Failed to send G-code"));
      }
    };

    


    function showPopup(message) {
      console.log("Attempting to show popup:", message);

      // If modal does not exist, create it
      if ($("#customPopup").length === 0) {
        $("body").append(`
              <div id="customPopup" class="modal fade" tabindex="-1" role="dialog">
                  <div class="modal-dialog modal-dialog-centered" role="document">
                      <div class="modal-content" style="border-radius: 10px; overflow: hidden;">
                          <div class="modal-header" style="background: linear-gradient(135deg, #007bff, #6610f2); color: white;">
                              <h5 class="modal-title">
                                  <i class="fas fa-info-circle"></i> Processing Request
                              </h5>
                              <button type="button" class="close" data-dismiss="modal" aria-label="Close" style="color: white; opacity: 0.8;">
                                  <span aria-hidden="true">&times;</span>
                              </button>
                          </div>
                          <div class="modal-body text-center">
                              <p id="customPopupMessage" class="mb-3" style="font-size: 16px; font-weight: 500;"></p>
                              <div class="spinner">
                                  <div class="double-bounce1"></div>
                                  <div class="double-bounce2"></div>
                              </div>
                          </div>
                      </div>
                  </div>
              </div>
          `);

        // Add custom CSS styles
        $("head").append(`
              <style>
                  .spinner {
                      width: 50px;
                      height: 50px;
                      position: relative;
                      margin: 0 auto;
                  }
  
                  .double-bounce1, .double-bounce2 {
                      width: 100%;
                      height: 100%;
                      border-radius: 50%;
                      background-color: #007bff;
                      opacity: 0.6;
                      position: absolute;
                      top: 0;
                      left: 0;
                      animation: bounce 2.0s infinite ease-in-out;
                  }
  
                  .double-bounce2 {
                      animation-delay: -1.0s;
                  }
  
                  @keyframes bounce {
                      0%, 100% { transform: scale(0.0); }
                      50% { transform: scale(1.0); }
                  }
              </style>
          `);
      }

      // Set the message and show the modal
      $("#customPopupMessage").text(message);
      $("#customPopup").modal("show");
    }

    function closePopup() {
      console.log("Closing popup...");
      $("#customPopup").modal("hide");
    }

    function closeErrorPopup() {
      console.log("Closing popup...");
      $("#errorPopup").modal("hide");
    }

    function showErrorPopup(message) {
      console.log("Attempting to show error popup:", message);

      // If modal does not exist, create it
      if ($("#errorPopup").length === 0) {
        $("body").append(`
              <div id="errorPopup" class="modal fade" tabindex="-1" role="dialog">
                  <div class="modal-dialog modal-dialog-centered" role="document">
                      <div class="modal-content" style="border-radius: 10px; overflow: hidden;">
                          <div class="modal-header" style="background: linear-gradient(135deg, #dc3545, #b30000); color: white;">
                              <h5 class="modal-title">
                                  <i class="fas fa-exclamation-triangle"></i> Error Occurred
                              </h5>
                              <button type="button" class="close" data-dismiss="modal" aria-label="Close" style="color: white; opacity: 0.8;">
                                  <span aria-hidden="true">&times;</span>
                              </button>
                          </div>
                          <div class="modal-body text-center">
                              <p id="errorPopupMessage" class="mb-3" style="font-size: 16px; font-weight: 500;"></p>
                              <div class="error-animation">
                                  <div class="circle">
                                      <div class="cross">
                                          <div class="cross-line"></div>
                                          <div class="cross-line"></div>
                                      </div>
                                  </div>
                              </div>
                          </div>
                      </div>
                  </div>
              </div>
          `);

        // Add custom CSS styles
        $("head").append(`
              <style>
                  .error-animation {
                      display: flex;
                      justify-content: center;
                      align-items: center;
                      width: 60px;
                      height: 60px;
                      margin: 10px auto;
                      position: relative;
                  }
  
                  .circle {
                      width: 60px;
                      height: 60px;
                      background-color: #dc3545;
                      border-radius: 50%;
                      display: flex;
                      justify-content: center;
                      align-items: center;
                      position: absolute;
                      animation: pulse 1.5s infinite;
                  }
  
                  .cross {
                      position: relative;
                      width: 40px;
                      height: 40px;
                  }
  
                  .cross-line {
                      position: absolute;
                      width: 35px;
                      height: 6px;
                      background-color: white;
                      border-radius: 5px;
                      top: 50%;
                      left: 50%;
                      transform: translate(-50%, -50%) rotate(45deg);
                  }
  
                  .cross-line:nth-child(2) {
                      transform: translate(-50%, -50%) rotate(-45deg);
                  }
  
                  @keyframes pulse {
                      0% { transform: scale(1); opacity: 0.7; }
                      50% { transform: scale(1.2); opacity: 0.5; }
                      100% { transform: scale(1); opacity: 0.7; }
                  }
              </style>
          `);
      }

      // Set the message and show the modal
      $("#errorPopupMessage").text(message);
      $("#errorPopup").modal("show");
    }





    self.onDataUpdaterPluginMessage = function (plugin, data) {
      if (plugin !== "Pinput_Shaping") return;

      if (data.type === "plotly_data") {
        // --- Clean up old plot ---
        Plotly.purge('plot_signal');
        Plotly.purge('plot_psd');
    
        // === SIGNAL GRAPH ===
        const traceRaw = {
          x: data.time,
          y: data.raw,
          mode: 'lines',
          name: 'Raw',
          line: { color: 'rgba(0,123,255,0.5)', width: 1.5 }
        };
    
        const traceFiltered = {
          x: data.time,
          y: data.filtered,
          mode: 'lines',
          name: 'Filtered',
          line: { color: 'orange', width: 2 }
        };
    
        const layoutSignal = {
          title: `Signal (Axis ${data.axis})`,
          xaxis: { title: 'Time (s)' },
          yaxis: { title: 'Acceleration (mm/s²)' },
          margin: { t: 40 }
        };
    
        Plotly.newPlot('plot_signal', [traceRaw, traceFiltered], layoutSignal, { responsive: true });
    
        // === PSD GRAPH ===
        const psdTraces = [
          {
            x: data.freqs,
            y: data.psd_original,
            mode: 'lines',
            name: 'Original PSD',
            line: { color: 'black', width: 1.5 }
          }
        ];
    
        for (const [name, shaper] of Object.entries(data.shapers)) {
          psdTraces.push({
            x: data.freqs,
            y: shaper.psd,
            mode: 'lines',
            name: `${name} (v=${shaper.vibr}, a=${shaper.accel})`,
            line: { dash: 'dash', width: 1.2 }
          });
        }
    
        const layoutPSD = {
          title: `PSD + Input Shapers (Axis ${data.axis})`,
          xaxis: { title: 'Frequency (Hz)', range: [0, 200] },
          yaxis: { title: 'Power Spectral Density' },
          margin: { t: 40 }
        };
    
        Plotly.newPlot('plot_psd', psdTraces, layoutPSD, { responsive: true });
      }
    
      
      if (data.type === "popup") {
        //console.log(">>> Showing popup with message:", data.message);
        showPopup(data.message);
      } else if (data.type === "close_popup") {
        closePopup();
      } else if (data.type === "error_popup") {
        showErrorPopup(data.message);
      } else if (data.type === "close_error_popup") {
        closeErrorPopup();
      }


      if (data.success === true) {
        self.error(null);
        self.summary(data.summary);
        self.samples(data.samples);
        self.hasData(true);
      } else if (data.error) {
        self.summary("");
        self.error("Error: " + data.error);
        self.hasData(false);
      } else {
        // If there is no error, nor show alert
        self.error(null);
      }


      if (data.results) {
        const table = [];
        for (let name in data.results) {
          const row = {
            name: name,
            vibration: parseFloat(data.results[name].vibr).toExponential(2),
            base_freq: parseFloat(data.base_freq).toFixed(2), 
            acceleration: parseFloat(data.results[name].accel).toFixed(1),
            isBest: name === data.best_shaper
          };
          table.push(row);
        }
        self.shaperResults(table);
      }


      if (data.signal_path && data.psd_path && data.command) {
        const plotsBase = "/plugin/Pinput_Shaping/static/metadata/";

        self.signalImgUrl(plotsBase + data.signal_path.split("/").pop());
        self.psdImgUrl(plotsBase + data.psd_path.split("/").pop());

        const csvName = data.csv_path.split("/").pop();
        self.csvFileUrl(plotsBase + csvName);

        self.recommendedCommand(data.command);
        self.showResults(true);
      }

      if (data.base_freq) {
        self.baseFreq(data.base_freq);
      }
      if (data.axis) {
        self.bestShaperAxis(data.axis);  
      }
    };

    self.signalPlotURL = ko.observable("");
    self.psdPlotURL = ko.observable("");
    self.csvPath = ko.observable("");
    self.m593Command = ko.observable("");
    self.baseFreq = ko.observable(null);
    self.bestShaperAxis = ko.observable("");
    self.dampingRatio = ko.observable("");

    self.downloadSignalPlot = function () {
      const link = document.createElement("a");
      link.href = self.signalPlotURL();
      link.download = "signal_plot.png";
      link.click();
    };

    self.downloadPSDPlot = function () {
      const link = document.createElement("a");
      link.href = self.psdPlotURL();
      link.download = "psd_plot.png";
      link.click();
    };

    self.downloadCSV = function () {
      if (!self.csvPath()) return;
      const link = document.createElement("a");
      link.href = self.csvPath();
      link.download = self.csvPath().split("/").pop();
      link.click();
    };

    self.sendM593Command = function () {
      if (!self.m593Command()) return;
      OctoPrint.control.sendGcode(self.m593Command())
        .done(() => alert("Command sent to printer!"))
        .fail(() => alert("Failed to send command."));
    };

    return self;
  }

  $('#imageModal').on('shown.bs.modal', function () {
    const img = document.getElementById('modalImage');
    const dialog = this.querySelector('.modal-dialog');
  
    function adjustModalSize() {
      const maxWidth = window.innerWidth * 0.95;
      const maxHeight = window.innerHeight * 0.95;
  
      const scaleX = maxWidth / img.naturalWidth;
      const scaleY = maxHeight / img.naturalHeight;
      const scale = Math.min(scaleX, scaleY, 1);
  
      const finalWidth = img.naturalWidth * scale;
      dialog.style.width = `${finalWidth}px`;
    }
  
    // Reset width before resizing
    dialog.style.width = "";
  
    if (img.complete) {
      // Image already loaded
      adjustModalSize();
    } else {
      img.onload = adjustModalSize;
    }
  
    // Force "Reload" for some browsers
    img.src = img.src;
  });

  OCTOPRINT_VIEWMODELS.push({
    construct: PinputShapingViewModel,
    dependencies: ["settingsViewModel"],
    elements: ["#pinput_shaping_tab", "#settings_plugin_Pinput_Shaping"]
  });

  console.log("✅ Pinput Shaping ViewModel registered");
});

