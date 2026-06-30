Create comprehensive documentation for a new feature or component:

1. Read `CLAUDE.md` and `agents/IMPLEMENTATION.md` to understand current state
2. Create feature documentation in `agents/plans/<feature>/`
3. Include:
   - **Overview**: Purpose and scope
   - **Architecture**: How it fits in the system
   - **State Management**: State machine usage (if applicable)
   - **API**: Endpoints and schemas (if applicable)
   - **Database**: Firestore collections and documents (if applicable)
   - **Testing**: Test strategy and coverage
   - **Dependencies**: External services or libraries
   - **Edge Cases**: Known limitations and gotchas
4. Update `agents/IMPLEMENTATION.md` with implementation status
5. Use references to code, not code duplication
6. Keep it concise and scannable (aim for 100-200 lines)

Focus on what developers need to know to work with this feature.
