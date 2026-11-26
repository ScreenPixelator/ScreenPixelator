# Changelog

## 1.1
- New quick choices (presets) that also set faces, darkness and strength: Hide nudity only, Women covered (men visible), Men covered (women visible), Everyone covered, Women fully covered (eyes only, 85% dark, 6 px blocks), Anonymous (eyes hidden). Choosing one applies it at once.
- Buttocks are covered in the women, men and women-fully-covered choices; with *Men covered, women visible*, buttocks of a recognised woman stay visible.
- No more *Save and apply* button: every change takes effect by itself, 2 seconds after the last change (also when closing the window right away).
- *Hide nudity only* is the new default for new installations, with milder strength (catch more nudity 80, people 65, margin 20%).
- *Hide only the eyes* now uses a wider band over the eyes.
- Every quick choice also sets the block size: 15 px (new default), 6 px for *Women fully covered*, with a margin of only 10% there.
- Smoother outlines with small blocks (no more square steps along bodies), and rounded instead of square zones around body parts.
- Smaller zones around breasts and buttocks: margin 20% by default (was 70%). Genitals keep at least 50%.
- The darkness warning starts at 80% (was 75%).
- Uninstalling no longer ends with a "has been removed" message that kept the app listed until you clicked OK.
- No more pink square flashing in the top left corner at every start: the check that the mosaic stays out of screen captures now runs once per Windows version.
- Includes the 1.0.1 fixes (version 1.0.1 was not published separately).

## 1.0.1
- Fix: the settings window could open as an empty little window. The password is now asked inside the window itself, and the check for an already open settings window no longer mixes it up with the pixelator's tray icon.
- Start, Stop and Save and apply now work in the background, so the settings window keeps responding (stopping can take several seconds when Windows is busy).
- Errors of the settings window are written to `settings.log` in the program folder.

## 1.0
- First public version.
