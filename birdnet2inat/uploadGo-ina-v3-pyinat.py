import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import re
from datetime import datetime
import webbrowser
import os
from pyinaturalist import get_taxa_autocomplete, create_observation
from tkcalendar import DateEntry  # pip install tkcalendar


class INatUploaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iNaturalist Audio Observation Uploader")
        self.geometry("800x950")

        self.file_path = None
        self.taxon_id = None
        self.observed_on = None
        self.observation_url = None
        self.species_name = None
        self.confidence = None

        self.create_widgets()

    def create_widgets(self):
        # Token entry
        tk.Label(self, text="Enter your iNaturalist API Token:").pack(pady=5)
        self.token_entry = tk.Entry(self, width=70, show="*")
        self.token_entry.pack(pady=5)

        tk.Button(self, text="Get iNaturalist API Token", command=self.open_token_page).pack(pady=5)

        # File selection
        tk.Button(self, text="Select audio file (.m4a)", command=self.select_file).pack(pady=10)
        self.lbl_file_info = tk.Label(self, text="No file selected")
        self.lbl_file_info.pack(pady=5)

        # --- Species input + search ---
        species_frame = tk.Frame(self)
        species_frame.pack(pady=10)
        tk.Label(species_frame, text="Species name:").grid(row=0, column=0, sticky="e")
        self.species_entry = tk.Entry(species_frame, width=40)
        self.species_entry.grid(row=0, column=1, padx=5)
        tk.Button(species_frame, text="Search", command=self.manual_search_species).grid(row=0, column=2, padx=5)

        self.lbl_species = tk.Label(self, text="Species match: None")
        self.lbl_species.pack(pady=5)

        # --- Date/time inputs ---
        datetime_frame = tk.Frame(self)
        datetime_frame.pack(pady=10)
        tk.Label(datetime_frame, text="Date:").grid(row=0, column=0, sticky="e")
        self.date_picker = DateEntry(datetime_frame, width=12, background='darkblue',
                                     foreground='white', borderwidth=2, date_pattern='yyyy-mm-dd')
        self.date_picker.grid(row=0, column=1, padx=5)

        tk.Label(datetime_frame, text="Time (HH:MM:SS):").grid(row=0, column=2, sticky="e")
        self.hour_spin = tk.Spinbox(datetime_frame, from_=0, to=23, width=3, format="%02.0f")
        self.minute_spin = tk.Spinbox(datetime_frame, from_=0, to=59, width=3, format="%02.0f")
        self.second_spin = tk.Spinbox(datetime_frame, from_=0, to=59, width=3, format="%02.0f")
        self.hour_spin.grid(row=0, column=3)
        self.minute_spin.grid(row=0, column=4)
        self.second_spin.grid(row=0, column=5)

        # --- Location inputs ---
        loc_frame = tk.Frame(self)
        loc_frame.pack(pady=10)

        tk.Label(loc_frame, text="Latitude:").grid(row=0, column=0, sticky="e")
        self.lat_entry = tk.Entry(loc_frame, width=15)
        self.lat_entry.grid(row=0, column=1, padx=5)
        self.lat_entry.insert(0, "49.5965")

        tk.Label(loc_frame, text="Longitude:").grid(row=0, column=2, sticky="e")
        self.lon_entry = tk.Entry(loc_frame, width=15)
        self.lon_entry.grid(row=0, column=3, padx=5)
        self.lon_entry.insert(0, "6.1423")

        tk.Label(loc_frame, text="Accuracy (m):").grid(row=0, column=4, sticky="e")
        self.acc_entry = tk.Entry(loc_frame, width=10)
        self.acc_entry.grid(row=0, column=5, padx=5)
        self.acc_entry.insert(0, "20")

        # --- Tags field ---
        tk.Label(self, text="Tags (comma-separated):").pack(pady=5)
        self.tags_entry = tk.Entry(self, width=80)
        self.tags_entry.insert(0, "sound recording, BirdNET-Go, BirdNET")
        self.tags_entry.pack(pady=5)

        # Comment box
        tk.Label(self, text="Observation comment:").pack(pady=5)
        self.txt_comment = tk.Text(self, height=4, width=80, state=tk.NORMAL)
        self.txt_comment.pack()

        # Upload button
        self.btn_upload = tk.Button(self, text="Upload Observation", command=self.upload_button_clicked, state=tk.DISABLED)
        self.btn_upload.pack(pady=10)

        # Upload status and URL
        self.lbl_upload_status = tk.Label(self, text="", font=('Arial', 10))
        self.lbl_upload_status.pack(pady=5)

        self.link_label = tk.Label(self, text="", fg="blue", cursor="hand2")
        self.link_label.pack(pady=5)

        # Log box
        tk.Label(self, text="API Logs:").pack(pady=5)
        self.log_box = scrolledtext.ScrolledText(self, width=90, height=15, state=tk.DISABLED)
        self.log_box.pack(padx=10, pady=5)

    def log(self, msg):
        self.log_box.config(state=tk.NORMAL)
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)

    def open_token_page(self):
        webbrowser.open("https://www.inaturalist.lu/users/api_token")

    def select_file(self):
        path = filedialog.askopenfilename(filetypes=[("Audio files", "*.m4a")])
        if not path:
            return
        self.file_path = path
        self.lbl_file_info.config(text=f"Selected: {os.path.basename(path)}")

        parsed = self.parse_filename(os.path.basename(path))
        if not parsed:
            messagebox.showinfo("Manual Input Required", "Filename not in standard format. Please fill manually.")
            self.btn_upload.config(state=tk.DISABLED)
            return

        species_name, confidence, observed_on = parsed
        self.species_entry.delete(0, tk.END)
        self.species_entry.insert(0, species_name)

        self.date_picker.set_date(observed_on.date())
        self.hour_spin.delete(0, tk.END)
        self.hour_spin.insert(0, observed_on.hour)
        self.minute_spin.delete(0, tk.END)
        self.minute_spin.insert(0, observed_on.minute)
        self.second_spin.delete(0, tk.END)
        self.second_spin.insert(0, observed_on.second)

        self.search_species(species_name, confidence)

    def parse_filename(self, filename):
        pattern = r"^([a-z_]+)_([0-9]+)p_(\d{8}T\d{6}Z)\.m4a$"
        m = re.match(pattern, filename)
        if not m:
            return None
        species_raw = m.group(1)
        confidence = int(m.group(2))
        timestamp_str = m.group(3)
        try:
            observed_on = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%SZ")
        except ValueError:
            return None
        species_name = " ".join(w.capitalize() for w in species_raw.split("_"))
        return species_name, confidence, observed_on

    def manual_search_species(self):
        name = self.species_entry.get().strip()
        if not name:
            messagebox.showerror("Missing Name", "Enter a species name to search.")
            return
        self.search_species(name, None)

    def search_species(self, species_name, confidence):
        token = self.token_entry.get().strip()
        if not token:
            messagebox.showerror("No Token", "Please enter your iNaturalist token first.")
            return

        self.log(f"Searching for '{species_name}'...")
        try:
            result = get_taxa_autocomplete(q=species_name, per_page=5, access_token=token)
            results = result.get("results", [])
            if not results:
                self.lbl_species.config(text=f"No match for '{species_name}'")
                self.btn_upload.config(state=tk.DISABLED)
                return

            best_match = results[0]
            taxon_id = best_match.get("id")
            matched_name = best_match.get("name")

            self.taxon_id = taxon_id
            self.species_name = matched_name
            self.confidence = confidence
            self.lbl_species.config(text=f"Match: {matched_name} (taxon_id: {taxon_id})")
            self.btn_upload.config(state=tk.NORMAL)

            comment = f"Automatic recording via BirdNet-Go. Initial ID via BirdNET soundscape analyzer: *{matched_name}*"
            if confidence is not None:
                comment += f", at a confidence of {confidence}%."
            else:
                comment += "."
            self.txt_comment.delete("1.0", tk.END)
            self.txt_comment.insert(tk.END, comment)
        except Exception as e:
            self.log(f"Error searching taxa: {e}")
            messagebox.showerror("Search Error", str(e))

    def upload_button_clicked(self):
        token = self.token_entry.get().strip()
        if not token or not self.file_path or not self.taxon_id:
            messagebox.showerror("Missing Data", "Token, file, or species missing.")
            return

        date_obj = self.date_picker.get_date()
        time_str = f"{int(self.hour_spin.get()):02}:{int(self.minute_spin.get()):02}:{int(self.second_spin.get()):02}"
        observed_on = datetime.strptime(f"{date_obj}T{time_str}", "%Y-%m-%dT%H:%M:%S")

        lat = float(self.lat_entry.get())
        lon = float(self.lon_entry.get())
        acc = float(self.acc_entry.get())
        comment = self.txt_comment.get("1.0", tk.END).strip()
        tag_list = self.tags_entry.get().strip()

        self.log("Uploading to iNaturalist...")
        try:
            response = create_observation(
                taxon_id=self.taxon_id,
                observed_on_string=observed_on.isoformat(),
                time_zone='Europe/Luxembourg',
                description=comment,
                tag_list=tag_list,
                latitude=lat,
                longitude=lon,
                positional_accuracy=acc,
                access_token=token,
                sounds=[self.file_path],
            )

            self.log(str(response))
            obs_id = response.get("id")
            if obs_id:
                self.observation_url = f"https://www.inaturalist.lu/observations/{obs_id}"
                messagebox.showinfo("Upload Successful", f"Observation uploaded!\n\nClick OK to open it.")
                self.lbl_upload_status.config(text="Upload successful ✅", fg="green")

                self.link_label.config(text=self.observation_url, fg="blue")
                self.link_label.bind("<Button-1>", lambda e: webbrowser.open_new(self.observation_url))
                webbrowser.open_new(self.observation_url)
            else:
                messagebox.showwarning("Upload Uncertain", "Upload complete, but no observation ID returned.")
                self.lbl_upload_status.config(text="Upload uncertain ⚠️", fg="orange")
        except Exception as e:
            self.log(f"Error during upload: {e}")
            messagebox.showerror("Upload Error", str(e))
            self.lbl_upload_status.config(text="Upload failed ❌", fg="red")


if __name__ == "__main__":
    app = INatUploaderApp()
    app.mainloop()
