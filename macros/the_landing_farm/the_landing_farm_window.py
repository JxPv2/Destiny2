import pyautogui
import time
import keyboard

# Define the keybind for the fast travel
keybind = {
    'fast_travel': '0',
}

# Function to perform the macro actions
def perform_macro(fast_travel):
    # Open map
    # Change according to your keybind
    pyautogui.press('f2')
    time.sleep(0.45) # Wait for the action to complete
    
    # Move the map to show the landing travel point
    pyautogui.moveTo(30, 671)
    time.sleep(0.75) # Wait for the action to complete
    
    # Stop the cursor on the landing travel point
    pyautogui.moveTo(260, 671)
    time.sleep(0.2) # Wait for the action to complete 
    
    # Left click
    pyautogui.mouseDown()
    time.sleep(1)# Wait for the action to complete
    pyautogui.mouseUp()

# Print the keybind
print("To change keybinds read README file")
print("")
for fast_travel, key in keybind.items():
    print(f"{fast_travel}: {key}")

def on_key_press(event):
    # Comment next 2 lines if not keypad keys
    if not event.is_keypad:
        return
    for fast_travel, key in keybind.items():
        if str(event.name) == key:
            perform_macro(fast_travel)
            
keyboard.on_press(on_key_press)

keyboard.wait("pause")
