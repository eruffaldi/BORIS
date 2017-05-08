
boris_ui4.py MISSING

boris_ui5.py
	actionRunEventOutside created
	added to self.menuObservations
	actionRunEventOutside setText 
boris.py
	def run_event_outside
 	actionRunEventOutside -> run_event_outside
 		enabled as check state: self.actionRunEventOutside.setEnabled(flag) inside menu_options
 	twEvents adds the action: self.twEvents.addAction(self.actionRunEventOutside) 


    def run_event_outside(self):
        if not self.observationId:
            self.no_observation()
            return

        if self.twEvents.selectedItems():        
            row = self.twEvents.selectedItems()[0].row()
            eventtime = self.pj[OBSERVATIONS][self.observationId][EVENTS][row][ 0 ]
            print (row,self.get_media_full_path(),eventtime)
            print (self.pj[OBSERVATIONS][self.observationId][EVENTS][row])

