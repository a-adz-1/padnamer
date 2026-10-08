#include <stddef.h>
#include <stdio.h>
#include <linux/input.h>
#include <linux/uinput.h>
#define P(x) printf(#x " %lu\n", (unsigned long)(x))
int main(){
P(sizeof(struct ff_effect)); P(sizeof(struct uinput_ff_upload)); P(sizeof(struct uinput_ff_erase));
P(sizeof(struct uinput_setup)); P(sizeof(struct uinput_abs_setup)); P(sizeof(struct input_event));
P(sizeof(struct input_absinfo)); P(offsetof(struct ff_effect,u)); P(offsetof(struct uinput_ff_upload,effect));
P(EVIOCGABS(3)); P(EVIOCSFF); P(EVIOCRMFF); P(EVIOCGEFFECTS); P(EVIOCGRAB);
P(UI_DEV_CREATE); P(UI_DEV_DESTROY); P(UI_DEV_SETUP); P(UI_ABS_SETUP); P(UI_SET_EVBIT); P(UI_SET_KEYBIT); P(UI_SET_FFBIT);
P(UI_SET_PHYS); P(UI_SET_PROPBIT); P(UI_SET_SWBIT); P(UI_BEGIN_FF_UPLOAD); P(UI_END_FF_UPLOAD); P(UI_BEGIN_FF_ERASE); P(UI_END_FF_ERASE);
P(EV_UINPUT); P(FF_RUMBLE); P(FF_GAIN); P(FF_AUTOCENTER); P(INPUT_PROP_ACCELEROMETER); P(BTN_MISC); P(KEY_MAX); P(EV_FF);
}
